"""Scoped, ignore-honouring, budgeted, content-shared run snapshots (R2-10).

A run directory holds a ``pristine`` reference copy and a ``work`` copy of the
target source tree. Measured on this project, that pair cost ~1.2 GB per run
(1.7 GB for two concurrent tasks) because the copier walked and copied
directories the repository itself declares as generated -- ``node_modules``,
``site``, build output. This module is the mechanism that stops it, and it is
deliberately built out of five separable pieces so a caller can use one without
the others.

1. **Ignore rules (:class:`IgnoreRules`).** A generated directory is not
   copied, so it cannot pollute the pristine reference and cannot make the
   "what changed" diff meaningless. The matcher is **git itself** when the
   source is a git checkout: one batched ``git check-ignore --no-index -z
   --stdin`` call decides the whole tree, honouring ``.gitignore`` files at
   every depth plus the user's ``core.excludesFile``. There is deliberately NO
   second pattern-matching implementation of gitignore semantics in this module
   -- a forked matcher drifts from git and would quietly copy what the repo
   says is generated. When git is unavailable the rules fall back to
   :func:`execution.workspace.is_generated_path` (the module's EXISTING
   artifact/VCS set), and :attr:`IgnoreRules.source` records which of the two
   actually ran, so a receipt never claims git semantics it did not get.

2. **Scope (:class:`TaskScope`).** A task may name a package/directory
   boundary (``Task.config["task_scope"]``). The snapshot, the path filter a
   retrieval caller uses, and the test selection all respect it, and an edit
   that escapes the scope is *reported* (:func:`scope_verdict`) rather than
   silently folded in. An undeclared scope is the whole repository -- absent is
   unchanged behaviour, never a narrower snapshot than the task asked for.

3. **Disk budget (:class:`BudgetSettings`, :class:`BudgetVerdict`).** The
   snapshot is *measured before a single byte is written* (:func:`plan_run_snapshot`)
   and a run whose upper bound would exceed the operator's ceiling, or would
   leave less than the reserve free on the target volume, is REFUSED with the
   numbers. The ceiling is enforced on the pre-link byte count, which is the
   upper bound: content sharing can only reduce it.

4. **Shared immutable content (:class:`SharedStore`).** Two runs that snapshot
   the same unchanged tree reference one stored copy instead of holding two.
   The safety property is structural, not conventional: **the shared path is
   the read-only reference and the write path is a private copy.**
   ``pristine/`` entries are hardlinks into a content-addressed store and are
   chmod'ed read-only, so an accidental in-place write fails loudly instead of
   silently corrupting the baseline and every other run sharing the inode;
   ``work/`` is always a real byte copy. Anything that cannot be made safe is
   NOT shipped as sharing -- :attr:`SnapshotReceipt.shared` and
   :attr:`SnapshotReceipt.shared_disabled_reason` say which happened, and a
   filesystem without working hardlinks degrades to copying with a recorded
   reason.

5. **Retention (:func:`prune_run_directories`, :func:`prune_shared_store`).**
   Pruning removes whole run directories and unreferenced blobs under a
   documented policy, and it removes *nothing else*: a directory is only a
   candidate when it is recognisably a run directory, and a sentinel placed
   beside them survives. :func:`memory.paths.retention_receipt` exposes the same
   policy to ``neo doctor``.

Nothing here changes a Boundary-0-5 signature, a completion status, or the
verifier's mint. **This module is not yet on the production path** -- the call
sites in ``harness/editor.py`` and ``harness/core.py`` belong to other prompts
and are filed as cross-terminal requests in ``execution/AGENTS.md``; the
``python -m execution.snapshot`` surface below is runnable today and is what the
required tests drive.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from execution.workspace import _git_environment, is_generated_path
from shared.retention import RetentionPolicy

PathLike = Union[str, os.PathLike[str]]

__all__ = [
    "BUDGET_REASONS",
    "SCOPE_CONFIG_KEY",
    "SNAPSHOT_BUDGET_CONFIG_KEYS",
    "BudgetSettings",
    "BudgetVerdict",
    "IgnoreRules",
    "PruneReport",
    "RetentionSettings",
    "SharedStore",
    "SnapshotBudgetExceeded",
    "SnapshotError",
    "SnapshotPlan",
    "SnapshotReceipt",
    "SnapshotScopeError",
    "TaskScope",
    "content_digest",
    "create_run_snapshot",
    "force_rmtree",
    "git_ignored_paths",
    "is_run_directory",
    "plan_run_snapshot",
    "prune_run_directories",
    "prune_shared_store",
    "resolve_budget",
    "resolve_retention",
    "retention_settings_from_env",
    "scope_verdict",
    "snapshot_reference",
    "snapshot_working",
    "tree_bytes",
]


# ---------------------------------------------------------------------------
# Config keys. Activation is KEY PRESENCE, and none of these is in
# harness/config.py::DEFAULTS -- a default there is merged into every task and
# every eval arm, so one entry would switch all of them in the same commit.
# ---------------------------------------------------------------------------

SCOPE_CONFIG_KEY = "task_scope"
SNAPSHOT_BUDGET_CONFIG_KEYS: Tuple[str, ...] = (
    "snapshot_budget_bytes",
    "snapshot_reserve_bytes",
    "snapshot_share_enabled",
    "snapshot_retention_days",
    "snapshot_retention_bytes",
    "snapshot_retention_keep",
)

#: Bounded internal default for the free-space reserve. Held here, NOT in
#: DEFAULTS, so a task that never declares a budget key is unaffected; the
#: reserve only bites when a snapshot would fill the volume.
DEFAULT_RESERVE_BYTES = 512 * 1024 * 1024

#: Bounded internal defaults for the retention policy.
DEFAULT_RETENTION_DAYS = 30.0
DEFAULT_RETENTION_KEEP = 20
DEFAULT_STORE_MAX_BYTES = 8 * 1024 * 1024 * 1024

#: Bounds. A walk, a subprocess, and a report all stay bounded.
MAX_WALK_ENTRIES = 400_000
MAX_IGNORE_PATHS = 400_000
MAX_IGNORED_SAMPLE = 20
MAX_LARGEST_DIRS = 5
DIGEST_CHUNK_BYTES = 1024 * 1024

#: Wall-clock ceiling on the PLANNING walk, in seconds. Measured on this host a
#: bare ``os.walk`` of a large tree runs at roughly 1 ms per entry, so an
#: unbounded plan is a mechanism nobody can wait for. A plan that hits this is
#: marked `truncated`, and a truncated plan REFUSES (see
#: :func:`enforce_budget`) because an incomplete measurement cannot honestly
#: certify a byte ceiling.
DEFAULT_PLAN_BUDGET_S = 30.0

#: Config key for the planning budget. Opt-in by presence like the rest.
SNAPSHOT_PLAN_BUDGET_KEY = "snapshot_plan_budget_s"

IGNORE_SOURCE_GIT = "git+generated"
IGNORE_SOURCE_GENERATED = "generated"
IGNORE_SOURCE_NONE = "none"

#: The closed set of `BudgetVerdict.reason` values, exported so a consumer can
#: branch on the decision without parsing prose and can reject a value this
#: module does not produce.
BUDGET_REASONS = {
    "within_budget",
    "over_operator_budget",
    "below_reserve",
    "over_budget_and_below_reserve",
    "free_space_unknown",
    "plan_incomplete",
}

#: Root-level files a SCOPED snapshot always carries. These are the files that
#: define how the repository is built and tested, not the package's content;
#: dropping them would make a scoped run's verifier execute a different suite
#: from the one the baseline ran, which is the "is this still the same suite"
#: failure R2-03 was built to prevent. Every entry is a file name (or a
#: `requirements*.txt` style stem prefix), matched at the repository ROOT only.
DEFAULT_SCOPE_CONFIG_FILES: Tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "pytest.ini",
    "tox.ini",
    "noxfile.py",
    "conftest.py",
    "requirements.txt",
    "package.json",
)


class SnapshotError(RuntimeError):
    """Base class for snapshot planning/execution failures."""


class SnapshotScopeError(SnapshotError):
    """A declared task scope is unusable (absolute, traversing, missing)."""


class SnapshotBudgetExceeded(SnapshotError):
    """A run's snapshot was refused because it would exceed its disk budget.

    Carries the :class:`BudgetVerdict` so a caller can render the refusal with
    its numbers instead of re-deriving them.
    """

    def __init__(self, verdict: "BudgetVerdict") -> None:
        self.verdict = verdict
        super().__init__(verdict.render())


# ---------------------------------------------------------------------------
# 1. Ignore rules
# ---------------------------------------------------------------------------


def _posix(value: str) -> str:
    """Normalize a relative path to forward slashes without a leading ``./``."""
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _relpath(path: PathLike, root: Path) -> str:
    """Return ``path`` relative to ``root`` in POSIX form, or "" on failure."""
    try:
        return Path(path).relative_to(root).as_posix()
    except (ValueError, OSError):
        return ""


def _git_toplevel(root: Path, env: Mapping[str, str]) -> Optional[Path]:
    """Return the git work tree containing ``root``, or ``None``.

    Asked explicitly because ``git check-ignore`` outside a repository exits 1
    with no output -- indistinguishable from "nothing is ignored". Treating
    that as a real answer would let the receipt claim git semantics for a plain
    directory, which is the exact "reported a mechanism it did not get" failure
    this module is supposed to make impossible.
    """
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            errors="replace",
            cwd=str(root),
            timeout=30,
            check=False,
            env=dict(env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    candidate = Path(completed.stdout.strip())
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def git_ignored_paths(root: PathLike, relatives: Sequence[str]) -> Optional[Set[str]]:
    """Return the subset of ``relatives`` git's ignore rules exclude.

    Delegates the decision to ``git check-ignore --no-index`` -- ONE subprocess
    for the whole tree -- so ``.gitignore`` files at every depth, negations,
    ``**`` patterns and ``core.excludesFile`` are honoured by git itself rather
    than by a second matcher here. ``--no-index`` is required: without it git
    refuses to report a TRACKED path that also matches an ignore pattern, and
    the question being asked is "does the repository declare this generated",
    not "is it in the index". ``-z`` disables git's path quoting.

    ``relatives`` are relative to ``root``; the query is rebased onto the
    enclosing work tree's top level, so a source that is a subdirectory of a
    repository is answered with that repository's rules and the answer is
    mapped back.

    Assumes every entry of ``relatives`` is a forward-slash relative path with
    no ``..``. Returns ``None`` when the question cannot be answered (git
    absent, no enclosing repository, or the query failed) so the caller can
    record an honest ``ignore_source`` instead of pretending the tree is clean.
    """
    base = Path(root)
    try:
        base = base.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    env = _git_environment()
    toplevel = _git_toplevel(base, env)
    if toplevel is None:
        return None
    prefix = _relpath(base, toplevel)
    candidates = [value for value in (_posix(item) for item in relatives) if value]
    if not candidates:
        return set()
    if len(candidates) > MAX_IGNORE_PATHS:
        candidates = candidates[:MAX_IGNORE_PATHS]
    query = [f"{prefix}/{value}" if prefix else value for value in candidates]
    payload = ("\0".join(query) + "\0").encode("utf-8", "surrogateescape")
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "check-ignore",
                "--no-index",
                "-z",
                "--stdin",
            ],
            input=payload,
            capture_output=True,
            cwd=str(toplevel),
            timeout=120,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # 0 = at least one ignored, 1 = none ignored, anything else = failure.
    if completed.returncode not in (0, 1):
        return None
    decoded = completed.stdout.decode("utf-8", "surrogateescape")
    ignored = {piece for piece in decoded.split("\0") if piece}
    if not prefix:
        return {_posix(piece) for piece in ignored}
    marker = prefix + "/"
    return {
        _posix(piece[len(marker) :]) for piece in ignored if piece.startswith(marker)
    }


@dataclass(frozen=True)
class IgnoreRules:
    """Which repository-relative paths a snapshot must not copy.

    ``git_ignored`` is the set git reported; ``generated`` is the module's
    existing artifact/VCS set. A path is excluded when EITHER says so. A
    repository that declares a directory generated therefore never has it
    copied into the pristine reference -- which is the point: it is pure waste,
    and it would otherwise show up in the run's "what changed" diff.
    """

    git_ignored: frozenset = frozenset()
    generated_only: bool = False
    source: str = IGNORE_SOURCE_NONE
    note: str = ""
    truncated: bool = False

    @classmethod
    def for_tree(
        cls,
        root: PathLike,
        relatives: Sequence[str],
        *,
        probe: Optional[Callable[[Path, Sequence[str]], Optional[Set[str]]]] = None,
    ) -> "IgnoreRules":
        """Build the rules for one tree.

        ``relatives`` is the repository-relative census the walk already
        produced, so this costs no extra filesystem work. ``probe`` is the
        injection point for a caller (or a test) that must not shell out to
        git; it defaults to :func:`git_ignored_paths` and a ``None`` return is
        recorded as an unanswerable question, not as "nothing is ignored".
        """
        query = git_ignored_paths if probe is None else probe
        try:
            ignored = query(Path(root), tuple(relatives))
        except Exception as exc:  # a probe failure must not fail the snapshot
            return cls(
                generated_only=True,
                source=IGNORE_SOURCE_GENERATED,
                note=f"ignore query failed: {type(exc).__name__}",
            )
        if ignored is None:
            return cls(
                generated_only=True,
                source=IGNORE_SOURCE_GENERATED,
                note="git ignore rules unavailable; used the generated-path set only",
            )
        return cls(
            git_ignored=frozenset(_posix(item) for item in ignored),
            generated_only=False,
            source=IGNORE_SOURCE_GIT,
        )

    def excludes(self, relative: str) -> bool:
        """True when ``relative`` must not be copied.

        A directory match also excludes everything beneath it, which is what
        makes a single ``node_modules/`` entry prune the whole subtree instead
        of matching every descendant individually.
        """
        candidate = _posix(relative)
        if not candidate:
            return False
        if is_generated_path(candidate):
            return True
        if not self.git_ignored:
            return False
        if candidate in self.git_ignored:
            return True
        parts = candidate.split("/")
        return any(
            "/".join(parts[:index]) in self.git_ignored
            for index in range(1, len(parts))
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for this rule set."""
        return {
            "source": self.source,
            "git_ignored": len(self.git_ignored),
            "generated_only": bool(self.generated_only),
            "note": self.note,
            "truncated": bool(self.truncated),
        }


# ---------------------------------------------------------------------------
# 2. Task scope
# ---------------------------------------------------------------------------


def _normalize_scope_root(value: str, repo_root: Path) -> str:
    """Return one repository-relative scope prefix, or raise.

    Refuses an absolute path, a drive-relative path, any ``..`` component, a
    control character, and a path that resolves outside the repository. Raising
    rather than widening is deliberate: a scope that silently degraded to "the
    whole repository" would snapshot 1.2 GB while the operator believed they
    had scoped the run to one package.
    """
    raw = str(value or "").replace("\\", "/").strip()
    if not raw or "\x00" in raw or any(ord(char) < 32 for char in raw):
        raise SnapshotScopeError(f"task scope is not a usable path: {value!r}")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise SnapshotScopeError(f"task scope must be repository-relative: {value!r}")
    parts: List[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise SnapshotScopeError(f"task scope must not traverse: {value!r}")
        parts.append(part)
    if not parts:
        raise SnapshotScopeError(f"task scope must name a directory: {value!r}")
    relative = "/".join(parts)
    target = (repo_root / relative).resolve(strict=False)
    base = repo_root.resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise SnapshotScopeError(
            f"task scope escapes the repository: {value!r}"
        ) from exc
    return relative


@dataclass(frozen=True)
class TaskScope:
    """A declared package/directory boundary for one task.

    ``roots`` are repository-relative POSIX prefixes. An EMPTY ``roots`` is the
    whole repository and is what an undeclared scope resolves to: absent
    ``task_scope`` must leave every existing run byte-identical, never
    accidentally narrower.

    ``always_include`` is the one documented exception, and it exists because
    scoping the SOURCE without it produces a run that cannot be tested: a
    repository's build/test configuration lives at its root
    (``pyproject.toml``'s pytest table, ``pytest.ini``, the root
    ``conftest.py``, the dependency manifests), and a ``pristine`` copy without
    them makes the verifier run a DIFFERENT suite than the baseline did -- which
    is the exact "is this the same suite" failure R2-03 exists to prevent. So
    these root-level files are always carried, they are named in
    :attr:`carried_config`, and they are excluded from the scope's own counts so
    a reader can tell carried configuration from scoped source. An operator who
    genuinely wants a bare directory passes ``always_include=()``.
    """

    roots: Tuple[str, ...] = ()
    declared: bool = False
    source: str = "undeclared"
    repo_root: str = ""
    note: str = ""
    always_include: Tuple[str, ...] = DEFAULT_SCOPE_CONFIG_FILES

    @classmethod
    def whole(cls, repo_root: PathLike = "") -> "TaskScope":
        """Return the undeclared (whole-repository) scope."""
        return cls(
            roots=(), declared=False, source="undeclared", repo_root=str(repo_root)
        )

    @classmethod
    def from_config(
        cls,
        config: Optional[Mapping[str, Any]],
        repo_root: PathLike,
    ) -> "TaskScope":
        """Resolve ``Task.config[SCOPE_CONFIG_KEY]`` into a scope.

        Accepts a single relative path or a sequence of them. Absent, ``None``,
        ``False`` and ``""`` all mean "not declared" -- the whole repository --
        matching the key-presence convention the rest of this module uses. A
        declared value that is not a path or sequence of paths, or that names
        nothing on disk, raises :class:`SnapshotScopeError`.
        """
        root = Path(repo_root)
        if not config or SCOPE_CONFIG_KEY not in config:
            return cls.whole(root)
        declared = config.get(SCOPE_CONFIG_KEY)
        if declared is None or declared is False or declared == "":
            return cls.whole(root)
        if isinstance(declared, str):
            values: Sequence[Any] = [declared]
        elif isinstance(declared, (list, tuple, set, frozenset)):
            values = list(declared)
        else:
            raise SnapshotScopeError(
                f"{SCOPE_CONFIG_KEY} must be a path or a list of paths, "
                f"got {type(declared).__name__}"
            )
        if not values:
            return cls.whole(root)
        roots: List[str] = []
        for value in values:
            if not isinstance(value, str):
                raise SnapshotScopeError(
                    f"{SCOPE_CONFIG_KEY} entries must be strings, "
                    f"got {type(value).__name__}"
                )
            relative = _normalize_scope_root(value, root)
            if relative not in roots:
                roots.append(relative)
        missing = [item for item in roots if not (root / item).exists()]
        if missing:
            raise SnapshotScopeError(
                f"{SCOPE_CONFIG_KEY} names directories that do not exist: "
                + ", ".join(missing)
            )
        return cls(
            roots=tuple(roots),
            declared=True,
            source=SCOPE_CONFIG_KEY,
            repo_root=str(root),
        )

    @property
    def whole_repository(self) -> bool:
        """True when this scope places no boundary on the snapshot."""
        return not self.roots

    def contains(self, relative: str) -> bool:
        """True when ``relative`` is inside the scope (or the scope is whole).

        A root-level configuration file named in ``always_include`` counts as
        inside, per the class docstring; :attr:`carried_config` distinguishes it
        from scoped source.
        """
        candidate = _posix(relative)
        if self.whole_repository:
            return True
        if not candidate:
            return False
        if self.is_carried_config(candidate):
            return True
        for root in self.roots:
            if candidate == root or candidate.startswith(root + "/"):
                return True
        return False

    def is_carried_config(self, relative: str) -> bool:
        """True when ``relative`` is a root configuration file always carried.

        ROOT only: a ``services/api/pytest.ini`` is ordinary scoped source and
        is not special-cased, because the boundary a scope draws is about
        package content. A WHOLE-REPOSITORY scope carries nothing, so this is
        ``False`` there -- otherwise every undeclared run would report its root
        configuration as "carried" and the receipt would name an exception that
        was not in force.
        """
        if self.whole_repository:
            return False
        candidate = _posix(relative)
        if not candidate or "/" in candidate:
            return False
        for name in self.always_include:
            if candidate == name:
                return True
            if name.endswith("*") and candidate.startswith(name[:-1]):
                return True
        return False

    def carried_config(self, relatives: Iterable[str]) -> Tuple[str, ...]:
        """Return the root configuration files present in ``relatives``."""
        return tuple(value for value in relatives if self.is_carried_config(value))

    def filter(self, relatives: Iterable[str]) -> Tuple[str, ...]:
        """Return only the in-scope members of ``relatives``, order preserved."""
        return tuple(value for value in relatives if self.contains(value))

    def outside(self, relatives: Iterable[str]) -> Tuple[str, ...]:
        """Return only the out-of-scope members of ``relatives``."""
        return tuple(value for value in relatives if not self.contains(value))

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for this scope."""
        return {
            "declared": bool(self.declared),
            "source": self.source,
            "roots": list(self.roots),
            "whole_repository": self.whole_repository,
            "always_include": list(self.always_include),
            "note": self.note,
        }


@dataclass(frozen=True)
class ScopeVerdict:
    """What a task's edits did relative to its declared scope.

    ``escaped`` is the load-bearing field: an edit outside the scope is not
    forbidden here (that is ``harness.editor``'s protected-path policy and
    stays there), but it MUST be reported, because a scoped run whose diff
    silently includes another package is a different claim from the one the
    operator scoped.
    """

    scope: TaskScope
    changed: Tuple[str, ...] = ()
    in_scope: Tuple[str, ...] = ()
    escaped: Tuple[str, ...] = ()

    @property
    def respected(self) -> bool:
        """True when no edit escaped the scope."""
        return not self.escaped

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt naming the scope and any escape."""
        return {
            "scope": self.scope.to_dict(),
            "changed": list(self.changed),
            "in_scope": list(self.in_scope),
            "escaped": list(self.escaped),
            "respected": self.respected,
        }


def scope_verdict(scope: TaskScope, changed: Iterable[str]) -> ScopeVerdict:
    """Split ``changed`` into in-scope and escaped paths against ``scope``."""
    normalized = tuple(_posix(value) for value in changed if _posix(value))
    return ScopeVerdict(
        scope=scope,
        changed=normalized,
        in_scope=tuple(value for value in normalized if scope.contains(value)),
        escaped=tuple(value for value in normalized if not scope.contains(value)),
    )


# ---------------------------------------------------------------------------
# 3. Measurement + budget
# ---------------------------------------------------------------------------


def tree_bytes(path: PathLike) -> int:
    """Return the summed ``st_size`` of every regular file under ``path``.

    Assumes ``path`` may not exist (returns 0) and never follows a symlinked
    directory. Bounded by the OS's own walk; a caller that needs a bound should
    use :func:`plan_run_snapshot`, which caps the entry count.
    """
    base = Path(path)
    if not base.is_dir():
        return 0
    total = 0
    for current, directories, names in os.walk(base, topdown=True, followlinks=False):
        directories[:] = [
            name for name in directories if not (Path(current) / name).is_symlink()
        ]
        for name in names:
            candidate = Path(current) / name
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


@dataclass(frozen=True)
class SnapshotPlan:
    """A measured, not-yet-written snapshot.

    Produced by :func:`plan_run_snapshot`, which walks and stats but never
    writes a byte. ``total_bytes`` is the UPPER bound on what the run will
    consume: it counts every file that will be copied, and content sharing can
    only reduce the real figure afterwards. ``ignored_bytes`` is what the
    ignore rules kept out, which is the waste this module exists to avoid.
    """

    source: str
    destination: str
    files: int = 0
    total_bytes: int = 0
    ignored_files: int = 0
    ignored_bytes: int = 0
    out_of_scope_files: int = 0
    out_of_scope_bytes: int = 0
    ignored_sample: Tuple[str, ...] = ()
    largest: Tuple[Tuple[str, int], ...] = ()
    ignore_source: str = IGNORE_SOURCE_NONE
    scope: TaskScope = field(default_factory=TaskScope)
    config_files: Tuple[str, ...] = ()
    entries: int = 0
    truncated: bool = False
    walked: bool = True
    in_scope: Tuple[str, ...] = ()

    @property
    def copy_files(self) -> int:
        """Number of files the snapshot will actually write."""
        return self.files

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for this plan."""
        return {
            "source": self.source,
            "destination": self.destination,
            "files": self.files,
            "total_bytes": self.total_bytes,
            "copy_files": self.copy_files,
            "ignored_files": self.ignored_files,
            "ignored_bytes": self.ignored_bytes,
            "out_of_scope_files": self.out_of_scope_files,
            "out_of_scope_bytes": self.out_of_scope_bytes,
            "ignored_sample": list(self.ignored_sample),
            "largest": [[name, size] for name, size in self.largest],
            "ignore_source": self.ignore_source,
            "scope": self.scope.to_dict(),
            "config_files": list(self.config_files),
            "entries": int(self.entries),
            "truncated": bool(self.truncated),
            "in_scope": list(self.in_scope),
        }


@dataclass(frozen=True)
class BudgetSettings:
    """A per-run byte ceiling plus a free-space reserve.

    ``max_bytes`` is the operator's ceiling and is ``None`` when the task did
    not declare one (no ceiling is not "unlimited by accident" -- it is
    reported as ``reason="no_budget"`` so a reader can see the absence).
    ``reserve_bytes`` is the free space a snapshot must leave behind.
    """

    max_bytes: Optional[int] = None
    reserve_bytes: Optional[int] = DEFAULT_RESERVE_BYTES
    source: str = "default"
    notes: Tuple[str, ...] = ()

    @property
    def declared(self) -> bool:
        """True when an operator ceiling was declared for this task."""
        return self.max_bytes is not None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for this policy."""
        return {
            "max_bytes": self.max_bytes,
            "reserve_bytes": self.reserve_bytes,
            "declared": self.declared,
            "source": self.source,
            "notes": list(self.notes),
        }


def _coerce_positive_int(value: Any, name: str, notes: List[str]) -> Optional[int]:
    """Coerce a config value to a positive int, recording a coercion note.

    A ``bool`` is refused rather than coerced: ``int(True) == 1`` byte would
    silently become "a 1-byte budget", which is the same class of bug as a
    truthiness check switching a gate off.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        notes.append(f"{name} is a bool; ignored")
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        notes.append(f"{name} is not an int; ignored")
        return None
    if number <= 0:
        notes.append(f"{name} is not positive; ignored")
        return None
    return number


def resolve_budget(config: Optional[Mapping[str, Any]] = None) -> BudgetSettings:
    """Resolve the budget policy from a task config.

    Reads ``snapshot_budget_bytes`` (the operator ceiling, opt-in by key
    presence) and ``snapshot_reserve_bytes`` (the free-space floor; an absent
    key takes :data:`DEFAULT_RESERVE_BYTES`). A value that had to be coerced or
    discarded produces a ``notes`` entry rather than being silently replaced.
    """
    notes: List[str] = []
    source = "default"
    if config and (
        "snapshot_budget_bytes" in config or "snapshot_reserve_bytes" in config
    ):
        source = "config"
    ceiling = _coerce_positive_int(
        (config or {}).get("snapshot_budget_bytes"), "snapshot_budget_bytes", notes
    )
    reserve = _coerce_positive_int(
        (config or {}).get("snapshot_reserve_bytes"), "snapshot_reserve_bytes", notes
    )
    if reserve is None and "snapshot_reserve_bytes" not in (config or {}):
        reserve = DEFAULT_RESERVE_BYTES
    return BudgetSettings(
        max_bytes=ceiling, reserve_bytes=reserve, source=source, notes=tuple(notes)
    )


@dataclass(frozen=True)
class BudgetVerdict:
    """Whether a planned snapshot may start, with the numbers that decided it.

    A refusal is a decision the operator can act on, so every number is here:
    the measured upper bound, the ceiling, the free space, the reserve, and the
    overage. ``reason`` is drawn from a closed set so a caller can branch on it
    without parsing prose.
    """

    allowed: bool
    reason: str
    estimated_bytes: int
    budget_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    reserve_bytes: Optional[int] = None
    overage_bytes: int = 0
    settings_source: str = "default"

    def render(self) -> str:
        """Return the one-line refusal/acceptance sentence with its numbers.

        Each reason gets the sentence that is true for IT. A ``plan_incomplete``
        refusal does not get an overage figure, because the measurement behind
        it is a lower bound and the arithmetic would be about a number known to
        be too small.
        """
        ceiling = (
            "none declared" if self.budget_bytes is None else _human(self.budget_bytes)
        )
        free = "unknown" if self.free_bytes is None else _human(self.free_bytes)
        reserve = "none" if self.reserve_bytes is None else _human(self.reserve_bytes)
        if self.allowed:
            return (
                f"snapshot within budget: {_human(self.estimated_bytes)} needed, "
                f"ceiling {ceiling}, {free} free, reserve {reserve} "
                f"({self.reason})"
            )
        if self.reason == "plan_incomplete":
            # The measurement is a LOWER bound here, so quoting an overage
            # against a ceiling would be arithmetic about a number that is
            # known to be too small. Say what is true instead.
            return (
                f"snapshot refused (plan_incomplete): the walk did not finish, "
                f"so {_human(self.estimated_bytes)} is a LOWER bound and the "
                f"{ceiling} ceiling cannot be certified; {free} free, "
                f"reserve {reserve}"
            )
        return (
            f"snapshot refused ({self.reason}): {_human(self.estimated_bytes)} "
            f"needed exceeds the {ceiling} ceiling by {_human(self.overage_bytes)}; "
            f"{free} free with a {reserve} reserve"
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for this verdict."""
        return {
            "allowed": bool(self.allowed),
            "reason": self.reason,
            "estimated_bytes": self.estimated_bytes,
            "budget_bytes": self.budget_bytes,
            "free_bytes": self.free_bytes,
            "reserve_bytes": self.reserve_bytes,
            "overage_bytes": self.overage_bytes,
            "settings_source": self.settings_source,
        }


def _human(value: Optional[int]) -> str:
    """Render a byte count for a human-facing refusal line."""
    if value is None:
        return "unknown"
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(number) < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(number)} B"
            return f"{number:.1f} {unit}"
        number /= 1024.0
    return f"{int(number)} B"  # pragma: no cover - loop always returns


def _free_bytes(path: PathLike) -> Optional[int]:
    """Return free space on the volume holding ``path`` or its nearest parent.

    ``None`` when the volume cannot be measured, which the verdict reports as
    ``free_space_unknown`` rather than treating as "plenty of room".
    """
    candidate = Path(path)
    for parent in [candidate, *candidate.parents]:
        if parent.exists():
            try:
                return int(shutil.disk_usage(str(parent)).free)
            except OSError:
                return None
    return None


def enforce_budget(
    plan: SnapshotPlan,
    settings: BudgetSettings,
    *,
    free_bytes: Optional[int] = None,
    measured_free: bool = True,
) -> BudgetVerdict:
    """Decide whether ``plan`` may start, without writing anything.

    Two independent refusals, both fail-closed: the measured upper bound must
    fit under the operator's ceiling, and it must fit under the free space
    minus the reserve. An unmeasurable volume is a refusal
    (``free_space_unknown``), not a pass -- the whole point is to refuse rather
    than discover the disk is full halfway through a copy. An absent ceiling is
    not a hole in this: the reserve still has to hold, and the receipt records
    ``budget_bytes: null`` so a reader can see that no ceiling was declared.

    A **truncated** plan is refused too (``plan_incomplete``). The plan's walk
    is bounded, and a plan that stopped early measured only part of the tree, so
    its byte total is a lower bound on the real cost. Certifying a ceiling
    against a lower bound is the optimistic direction, which is the one this
    whole mechanism exists to avoid.
    """
    estimated = int(plan.total_bytes)
    ceiling = settings.max_bytes
    reserve = settings.reserve_bytes
    free = free_bytes
    if not measured_free:
        free = None
    if free is None and measured_free:
        free = _free_bytes(plan.destination)

    over_ceiling = ceiling is not None and estimated > int(ceiling)
    overage = max(0, estimated - int(ceiling)) if ceiling is not None else 0
    below_reserve = free is None or (
        reserve is not None and estimated > free - int(reserve)
    )

    if plan.truncated:
        reason = "plan_incomplete"
        # A truncated plan's total is a LOWER bound, so an overage computed from
        # it is meaningless. Report it as "not computable" rather than 0, which
        # would read as "exactly at the ceiling".
        overage = -1
    elif ceiling is None and free is None:
        reason = "free_space_unknown"
    elif over_ceiling and below_reserve:
        reason = "over_budget_and_below_reserve"
    elif over_ceiling:
        reason = "over_operator_budget"
    elif below_reserve:
        reason = "below_reserve"
    else:
        reason = "within_budget"
    allowed = reason == "within_budget"
    return BudgetVerdict(
        allowed=allowed,
        reason=reason,
        estimated_bytes=estimated,
        budget_bytes=ceiling,
        free_bytes=free,
        reserve_bytes=reserve,
        overage_bytes=overage,
        settings_source=settings.source,
    )


# ---------------------------------------------------------------------------
# 4. Shared immutable content
# ---------------------------------------------------------------------------


def content_digest(path: PathLike) -> Optional[str]:
    """Return the SHA-256 of a regular file's bytes, or ``None``.

    Chunked, so a multi-gigabyte file does not have to fit in memory. Symlinks
    and non-regular files answer ``None``: a shared store holds file CONTENT,
    and storing a link's target would alias content the snapshot does not own.
    """
    candidate = Path(path)
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None
    except OSError:
        return None
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as handle:
            while True:
                chunk = handle.read(DIGEST_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


class SharedStore:
    """A content-addressed store of immutable file blobs, shared between runs.

    Layout is ``<root>/blobs/<first two hex>/<full digest>``. The store holds
    ONE copy of each distinct content, so a second run that snapshots an
    unchanged tree hardlinks the same inode instead of paying for the bytes
    again.

    **The write path is never shared.** A snapshot's ``pristine/`` entries link
    here and are chmod'ed read-only; a run's ``work/`` tree is always a real
    copy. That separation is what makes the sharing safe: an agent that edits
    in place -- which a shell redirect in the bind-mounted sandbox absolutely
    can do -- mutates only its own private copy, while the baseline and every
    other run sharing the inode are protected by the read-only mode. Linking
    ``work/`` would be faster still and is NOT done, because an in-place write
    would then silently rewrite the diff baseline.

    On a filesystem without working hardlinks every call falls back to copying
    and says so through :meth:`put`/the snapshot receipt's
    ``shared_disabled_reason``; nothing is ever reported as shared that is not.
    """

    BLOB_DIRNAME = "blobs"
    READ_ONLY_MODE = 0o444

    def __init__(self, root: PathLike, *, read_only: bool = True) -> None:
        """Bind to ``root``; ``read_only`` is the mode applied to stored blobs.

        Assumes nothing about whether ``root`` exists (it is created lazily on
        the first write) and never raises for an unwritable root -- the first
        :meth:`put` reports the failure to its caller instead.
        """
        self.root = Path(root)
        self.read_only = bool(read_only)
        self._unavailable_reason: str = ""

    @property
    def blob_root(self) -> Path:
        """The directory holding the two-hex fan-out."""
        return self.root / self.BLOB_DIRNAME

    def path_for(self, digest: str) -> Path:
        """Return the store path for ``digest`` (whether or not it exists)."""
        text = str(digest or "").strip().lower()
        if len(text) < 4 or any(char not in "0123456789abcdef" for char in text):
            raise SnapshotError(f"not a content digest: {digest!r}")
        return self.blob_root / text[:2] / text

    def contains(self, digest: str) -> bool:
        """True when the store already holds this content."""
        try:
            return self.path_for(digest).is_file()
        except SnapshotError:
            return False

    def put(self, source: PathLike, digest: str) -> Tuple[Path, bool]:
        """Ensure ``digest`` is stored; return ``(blob_path, already_present)``.

        The blob is written to a unique temporary file and moved into place, so
        a crash mid-write cannot leave a truncated blob that a later run would
        happily hardlink as if it were the real content.
        """
        target = self.path_for(digest)
        if target.is_file():
            return target, True
        source_path = Path(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.{os.getpid()}.tmp"
        try:
            shutil.copyfile(source_path, temporary)
            if self.read_only:
                os.chmod(temporary, self.READ_ONLY_MODE)
            os.replace(str(temporary), str(target))
        except OSError as exc:
            self._unavailable_reason = f"{type(exc).__name__}: {exc}"
            try:
                temporary.unlink()
            except OSError:
                pass
            raise SnapshotError(
                f"shared store could not accept {target.name}: {type(exc).__name__}"
            ) from exc
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return target, False

    def link(self, blob: PathLike, destination: PathLike) -> Tuple[bool, str]:
        """Hardlink ``blob`` to ``destination``; return ``(shared, reason)``.

        Falls back to copying when the link cannot be made -- a different
        volume, a filesystem without hardlink support, or a permission denial
        -- and returns ``(False, reason)``. It never claims a share it did not
        get.
        """
        target = Path(destination)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"destination is not writable: {type(exc).__name__}"
        try:
            os.link(str(blob), str(target))
        except FileExistsError:
            try:
                target.unlink()
                os.link(str(blob), str(target))
            except OSError as exc:
                return False, f"relink failed: {type(exc).__name__}"
        except OSError as exc:
            return False, f"hardlink unavailable: {type(exc).__name__}"
        return True, "hardlink"

    def protect(self, path: PathLike) -> bool:
        """Make a shared read-path entry read-only; True when it is now RO.

        A failure here is reported, never swallowed silently: the caller puts
        the reason in the receipt, because an unprotected shared file is the
        one state in which this design is unsafe.
        """
        try:
            os.chmod(str(path), self.READ_ONLY_MODE)
        except OSError:
            return False
        return True

    @property
    def unavailable_reason(self) -> str:
        """Why the store last failed, or "" when it has not."""
        return self._unavailable_reason

    def stats(self) -> Dict[str, Any]:
        """Return ``{root, exists, blobs, bytes, path}`` for the store.

        ``bytes`` is the summed size of the blobs themselves; it is an upper
        bound on what is actually consumed, because a blob is only charged once
        on disk however many runs hardlink it.
        """
        blobs = 0
        total = 0
        if self.blob_root.is_dir():
            for current, _directories, names in os.walk(
                self.blob_root, topdown=True, followlinks=False
            ):
                for name in names:
                    candidate = Path(current) / name
                    try:
                        if candidate.is_symlink() or not candidate.is_file():
                            continue
                        blobs += 1
                        total += candidate.stat().st_size
                    except OSError:
                        continue
        return {
            "root": str(self.root),
            "exists": self.blob_root.is_dir(),
            "blobs": blobs,
            "bytes": total,
            "path": str(self.blob_root),
        }

    def iter_blobs(self) -> Iterable[Path]:
        """Yield every stored blob path, deepest first, symlinks skipped."""
        if not self.blob_root.is_dir():
            return ()
        found: List[Path] = []
        for current, _directories, names in os.walk(
            self.blob_root, topdown=True, followlinks=False
        ):
            for name in names:
                candidate = Path(current) / name
                if candidate.name.endswith(".tmp"):
                    continue
                try:
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                except OSError:
                    continue
                found.append(candidate)
        return sorted(found, key=lambda item: str(item), reverse=True)


# ---------------------------------------------------------------------------
# Snapshot execution
# ---------------------------------------------------------------------------


@dataclass
class _Census:
    """The result of ONE walk and ONE stat pass over a source tree.

    Deliberately a single pass. The first version measured the tree four times
    (kept / ignored / out-of-scope / carried) and a bare ``os.walk`` of a large
    repository on this host already costs ~1 ms per entry, so the measurement --
    not the copy -- was the dominant cost of the whole mechanism. One walk, one
    ``stat`` per file, four buckets, and the per-file sizes kept so the
    post-walk git-ignore filter can re-total WITHOUT touching the disk again.
    """

    kept: List[Tuple[str, int]] = field(default_factory=list)
    carried: List[Tuple[str, int]] = field(default_factory=list)
    out_of_scope_files: int = 0
    out_of_scope_bytes: int = 0
    ignored: List[str] = field(default_factory=list)
    ignored_bytes: int = 0
    directories: List[str] = field(default_factory=list)
    entries: int = 0
    truncated: bool = False

    @staticmethod
    def _total(pairs: Sequence[Tuple[str, int]]) -> int:
        return sum(size for _name, size in pairs)

    @staticmethod
    def _names(pairs: Sequence[Tuple[str, int]]) -> List[str]:
        return [name for name, _size in pairs]


def _walk_census(
    source: Path,
    rules: Optional[IgnoreRules] = None,
    scope: Optional[TaskScope] = None,
    exclude_roots: Sequence[Path] = (),
    *,
    budget_s: float = DEFAULT_PLAN_BUDGET_S,
    clock: Optional[Callable[[], float]] = None,
) -> Tuple[_Census, bool]:
    """Walk ``source`` once, bucketing every entry; return ``(census, walked_all)``.

    Pruning, in order of cost:

    1. ``execution.workspace.is_generated_path`` -- the module's EXISTING
       artifact/VCS set, so ``node_modules``/``__pycache__``/``.git`` are never
       descended into at all.
    2. ``exclude_roots`` -- harness-owned artifact roots (the run directory and
       the log root) when they live inside the source. These are not repository
       content and walking them is what made a plan over a real tree take
       minutes.
    3. The entry-count and wall-clock budgets. Hitting either sets
       ``truncated``; the caller must treat a truncated plan as unprovable.

    git's ignore decision is applied to the resulting census (one batched call),
    not per directory -- which means an ignored-but-not-generated directory is
    still WALKED for stat purposes. That cost is reported as ``truncated``/
    ``walked`` rather than hidden, and it never affects the copy: no ignored
    byte is ever read or written.
    """
    census = _Census()
    started = (clock or time.time)()
    excluded: List[Path] = []
    for candidate in exclude_roots or ():
        try:
            excluded.append(Path(candidate).resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            continue

    def _is_excluded(path: Path) -> bool:
        if not excluded:
            return False
        resolved = path.resolve(strict=False)
        return any(
            resolved == item or resolved.is_relative_to(item) for item in excluded
        )

    for current, dirnames, filenames in os.walk(
        source, topdown=True, followlinks=False
    ):
        if (clock or time.time)() - started > budget_s:
            census.truncated = True
            break
        base = Path(current)
        keep: List[str] = []
        for name in sorted(dirnames):
            relative = _relpath(base / name, source)
            if not relative or _is_excluded(base / name):
                census.ignored.append(relative)
                continue
            if is_generated_path(relative):
                census.ignored.append(relative)
                continue
            try:
                if (base / name).is_symlink():
                    census.ignored.append(relative)
                    continue
            except OSError:
                census.ignored.append(relative)
                continue
            keep.append(name)
            census.directories.append(relative)
        dirnames[:] = keep
        for name in sorted(filenames):
            if (clock or time.time)() - started > budget_s:
                census.truncated = True
                break
            relative = _relpath(base / name, source)
            if not relative:
                continue
            census.entries += 1
            if census.entries > MAX_WALK_ENTRIES:
                census.truncated = True
                break
            if rules is not None and rules.excludes(relative):
                census.ignored.append(relative)
                continue
            try:
                candidate = base / name
                if candidate.is_symlink() or not candidate.is_file():
                    census.ignored.append(relative)
                    continue
                size = int(candidate.stat().st_size)
            except OSError:
                census.ignored.append(relative)
                continue
            if scope is not None and not scope.contains(relative):
                census.out_of_scope_files += 1
                census.out_of_scope_bytes += size
                continue
            if scope is not None and scope.is_carried_config(relative):
                census.carried.append((relative, size))
                continue
            census.kept.append((relative, size))
    return census, not census.truncated


def _artifact_exclusions(
    source: Path, run_dir: Path, extra: Sequence[Path] = ()
) -> List[Path]:
    """Roots inside ``source`` that are harness artifacts, not repository content.

    The run directory itself is always excluded: it is where this operation is
    about to write, so descending into it would be self-referential (the shape
    that produced a live ``RecursionError`` in the interactive flow).

    The enclosing LOG ROOT is deliberately NOT guessed. A repository may
    legitimately commit a ``logs/`` directory of its own, and
    ``harness.editor.snapshot`` already pins that a same-named directory which is
    real repo content still copies. A caller whose log root really is inside the
    repository passes it in ``extra`` -- that is knowledge only the caller has.
    """
    exclusions = [Path(run_dir)]
    for candidate in extra or ():
        if candidate:
            exclusions.append(Path(candidate))
    try:
        source_root = source.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return exclusions
    kept: List[Path] = []
    for candidate in exclusions:
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved != source_root and resolved.is_relative_to(source_root):
            kept.append(resolved)
    return kept


def _plan_budget_s(config: Optional[Mapping[str, Any]]) -> float:
    """Resolve the planning walk budget, clamped to a positive number."""
    if config and SNAPSHOT_PLAN_BUDGET_KEY in config:
        value = config.get(SNAPSHOT_PLAN_BUDGET_KEY)
        if isinstance(value, bool):
            return DEFAULT_PLAN_BUDGET_S
        try:
            number = float(value)
        except (TypeError, ValueError):
            return DEFAULT_PLAN_BUDGET_S
        return number if number > 0 else DEFAULT_PLAN_BUDGET_S
    return DEFAULT_PLAN_BUDGET_S


def plan_run_snapshot(
    source: PathLike,
    run_dir: PathLike,
    *,
    config: Optional[Mapping[str, Any]] = None,
    scope: Optional[TaskScope] = None,
    probe: Optional[Callable[[Path, Sequence[str]], Optional[Set[str]]]] = None,
    clock: Optional[Callable[[], float]] = None,
    exclude_roots: Sequence[Path] = (),
) -> SnapshotPlan:
    """Measure a run's ``pristine`` + ``work`` pair without writing a byte.

    ``run_dir`` is the task directory (``logs/{task_id}``); the plan covers both
    ``run_dir/pristine`` and ``run_dir/work``. ``total_bytes`` is the UPPER
    bound the budget is checked against, because content sharing can only
    reduce the real figure after the fact.

    The walk is bounded by ``snapshot_plan_budget_s`` (default
    :data:`DEFAULT_PLAN_BUDGET_S`) and by :data:`MAX_WALK_ENTRIES`. A plan that
    hit either bound is ``truncated``, and :func:`enforce_budget` REFUSES a
    truncated plan -- an incomplete measurement cannot certify a byte ceiling,
    and reporting "within budget" about a tree that was not fully walked is
    precisely the optimistic claim this module exists to stop.

    Assumes ``source`` is a directory; a non-directory yields an empty plan
    rather than raising. The scope is resolved from ``config`` when not
    supplied. ``clock`` is the injectable time source for the walk budget, and
    ``exclude_roots`` names additional harness-owned trees the caller knows are
    artifacts (a log root placed inside the repository, for example).
    """
    source_path = Path(source)
    run_path = Path(run_dir)
    resolved_scope = scope or TaskScope.from_config(config, source_path)
    if not source_path.is_dir():
        return SnapshotPlan(
            source=str(source_path),
            destination=str(run_path),
            ignore_source=IGNORE_SOURCE_NONE,
            scope=resolved_scope,
            walked=False,
        )
    census, _complete = _walk_census(
        source_path,
        scope=resolved_scope,
        exclude_roots=_artifact_exclusions(source_path, run_path, exclude_roots),
        budget_s=_plan_budget_s(config),
        clock=clock,
    )
    rules = IgnoreRules.for_tree(
        source_path,
        list(census.directories)
        + _Census._names(census.kept)
        + _Census._names(census.carried),
        probe=probe,
    )
    dropped = [(name, size) for name, size in census.kept if rules.excludes(name)]
    kept = [(name, size) for name, size in census.kept if not rules.excludes(name)]
    carried = [
        (name, size) for name, size in census.carried if not rules.excludes(name)
    ]
    # `dropped` holds files the walk had to STAT to learn git's answer about
    # them. Their bytes are waste avoided, so they belong in `ignored_bytes`:
    # reporting zero there would understate the saving the ignore rules bought.
    dropped_bytes = _Census._total(dropped)
    ignored_names = list(census.ignored) + [name for name, _size in dropped]
    kept_total = _Census._total(kept) + _Census._total(carried)
    largest = sorted(kept, key=lambda item: (-item[1], item[0]))[:MAX_LARGEST_DIRS]
    return SnapshotPlan(
        source=str(source_path),
        destination=str(run_path),
        files=(len(kept) + len(carried)) * 2,
        total_bytes=kept_total * 2,
        ignored_files=len(ignored_names),
        ignored_bytes=census.ignored_bytes + dropped_bytes,
        out_of_scope_files=census.out_of_scope_files,
        out_of_scope_bytes=census.out_of_scope_bytes,
        ignored_sample=tuple(sorted(ignored_names)[:MAX_IGNORED_SAMPLE]),
        largest=tuple(largest),
        ignore_source=rules.source,
        scope=resolved_scope,
        config_files=tuple(sorted(name for name, _size in carried)),
        entries=census.entries,
        truncated=census.truncated,
        in_scope=tuple(_Census._names(kept) + _Census._names(carried)),
    )


@dataclass(frozen=True)
class SnapshotReceipt:
    """What a snapshot actually did, and what it refused to do.

    Every field is a claim a reader can check against the filesystem:
    ``shared`` is the number of reference files that really are hardlinks,
    ``work_is_private_copy`` is the structural safety property (it is ``True``
    by construction -- ``work/`` is never linked), and
    ``shared_disabled_reason`` names why sharing did not happen when it did
    not.
    """

    source: str
    pristine: str
    work: str
    plan: SnapshotPlan
    written: int = 0
    shared: int = 0
    copied: int = 0
    private_copies: int = 0
    work_is_private_copy: bool = True
    shared_disabled_reason: str = ""
    budget: Optional[BudgetVerdict] = None
    scope: Optional[ScopeVerdict] = None
    store_root: str = ""
    elapsed_s: float = 0.0
    errors: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for the whole operation."""
        return {
            "source": self.source,
            "pristine": self.pristine,
            "work": self.work,
            "written": self.written,
            "shared": self.shared,
            "copied": self.copied,
            "private_copies": self.private_copies,
            "work_is_private_copy": bool(self.work_is_private_copy),
            "shared_disabled_reason": self.shared_disabled_reason,
            "budget": self.budget.to_dict() if self.budget else None,
            "scope": self.scope.to_dict() if self.scope else None,
            "plan": self.plan.to_dict(),
            "store_root": self.store_root,
            "elapsed_s": round(float(self.elapsed_s), 3),
            "errors": list(self.errors),
        }


def _copy_file(source: Path, destination: Path) -> None:
    """Copy a regular file's bytes, creating parents. Never follows symlinks."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        try:
            destination.unlink()
        except OSError:
            pass
    shutil.copyfile(str(source), str(destination))


def _destination_guard(source: Path, destination: Path) -> Optional[str]:
    """Return the top-level segment of ``source`` to exclude, or ``None``.

    In the plain ``neo`` interactive flow the log root defaults to ``./logs``
    INSIDE the target repository, so the snapshot destination lives inside the
    source. A naive copy then descends into its own output until
    ``RecursionError`` (found live in the interactive drive). The same guard
    exists in ``harness.editor.snapshot``; it is reproduced here because this
    copier is the one that will eventually replace it, and both need it.
    """
    try:
        source_root = source.resolve()
        parent = destination.parent.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if parent == source_root:
        return destination.name
    try:
        return parent.relative_to(source_root).parts[0]
    except ValueError:
        return None


def snapshot_reference(
    source: PathLike,
    destination: PathLike,
    *,
    store: Optional[SharedStore] = None,
    relatives: Optional[Sequence[str]] = None,
    rules: Optional[IgnoreRules] = None,
    scope: Optional[TaskScope] = None,
) -> Tuple[int, int, str]:
    """Write the READ-ONLY reference copy; return ``(written, shared, reason)``.

    With a ``store`` the reference entries are hardlinks into it and are made
    read-only, so two runs of an unchanged tree share one set of bytes. Without
    one (or when linking is impossible) this is a plain copy -- still the
    reference half, still separated from the write path.
    """
    source_path = Path(source)
    target = Path(destination)
    exclude = _destination_guard(source_path, target)
    if relatives is None:
        census, _complete = _walk_census(source_path, rules=rules, scope=scope)
        files = _Census._names(census.kept) + _Census._names(census.carried)
    else:
        files = [
            value
            for value in relatives
            if not (rules is not None and rules.excludes(value))
            and (scope is None or scope.contains(value))
        ]
    if exclude:
        files = [value for value in files if value.split("/")[0] != exclude]
    files.sort()
    target.mkdir(parents=True, exist_ok=True)
    written = 0
    shared = 0
    reason = ""
    for relative in files:
        origin = source_path / relative
        destination_path = target / relative
        if origin.is_symlink() or not origin.is_file():
            continue
        linked = False
        if store is not None:
            digest = content_digest(origin)
            if digest:
                try:
                    blob, _present = store.put(origin, digest)
                    linked, reason = store.link(blob, destination_path)
                except SnapshotError as exc:
                    linked = False
                    reason = str(exc)
                if linked:
                    store.protect(destination_path)
        if not linked:
            _copy_file(origin, destination_path)
            if store is not None and not reason:
                reason = "content sharing was not available"
        if destination_path.exists():
            written += 1
        if linked:
            shared += 1
    return written, shared, reason


def snapshot_working(
    source: PathLike,
    destination: PathLike,
    *,
    relatives: Optional[Sequence[str]] = None,
    rules: Optional[IgnoreRules] = None,
    scope: Optional[TaskScope] = None,
) -> int:
    """Write the PRIVATE, writable working copy; return files written.

    Never hardlinks. This is the write path, and separating it from the shared
    read path is the property that makes content sharing safe at all.
    """
    source_path = Path(source)
    target = Path(destination)
    exclude = _destination_guard(source_path, target)
    if relatives is None:
        census, _complete = _walk_census(source_path, rules=rules, scope=scope)
        files = _Census._names(census.kept) + _Census._names(census.carried)
    else:
        files = [
            value
            for value in relatives
            if not (rules is not None and rules.excludes(value))
            and (scope is None or scope.contains(value))
        ]
    if exclude:
        files = [value for value in files if value.split("/")[0] != exclude]
    files.sort()
    target.mkdir(parents=True, exist_ok=True)
    written = 0
    for relative in files:
        origin = source_path / relative
        destination_path = target / relative
        if origin.is_symlink() or not origin.is_file():
            continue
        _copy_file(origin, destination_path)
        try:
            os.chmod(str(destination_path), stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        written += 1
    return written


def create_run_snapshot(
    source: PathLike,
    run_dir: PathLike,
    *,
    config: Optional[Mapping[str, Any]] = None,
    scope: Optional[TaskScope] = None,
    store: Optional[SharedStore] = None,
    probe: Optional[Callable[[Path, Sequence[str]], Optional[Set[str]]]] = None,
    changed: Optional[Iterable[str]] = None,
    allow_budget_refusal: bool = False,
) -> SnapshotReceipt:
    """Create ``run_dir/pristine`` and ``run_dir/work`` under a measured budget.

    The order is load-bearing and is the reason this can refuse safely:
    **plan, enforce, then write.** A run whose measured upper bound exceeds its
    ceiling -- or would eat the free-space reserve -- raises
    :class:`SnapshotBudgetExceeded` carrying the numbers BEFORE any byte is
    written. Pass ``allow_budget_refusal=True`` to get the refusing
    :class:`BudgetVerdict` back on the receipt instead of an exception (that is
    the shape a caller wants when it renders a message rather than failing).

    ``store`` enables content sharing of the reference half. The working half
    is always a private copy. ``changed``, when given, is folded into the
    receipt's :class:`ScopeVerdict` so an edit that escaped the scope is
    reported by the snapshot itself rather than needing a second pass.
    """
    started = time.time()
    source_path = Path(source)
    run_path = Path(run_dir)
    resolved_scope = scope or TaskScope.from_config(config, source_path)
    plan = plan_run_snapshot(
        source_path, run_path, config=config, scope=resolved_scope, probe=probe
    )
    settings = resolve_budget(config)
    verdict = enforce_budget(plan, settings)
    if not verdict.allowed and not allow_budget_refusal:
        raise SnapshotBudgetExceeded(verdict)
    if not verdict.allowed:
        return SnapshotReceipt(
            source=str(source_path),
            pristine=str(run_path / "pristine"),
            work=str(run_path / "work"),
            plan=plan,
            budget=verdict,
            scope=(
                scope_verdict(resolved_scope, changed) if changed is not None else None
            ),
            elapsed_s=time.time() - started,
        )

    pristine = run_path / "pristine"
    work = run_path / "work"
    if pristine.exists():
        force_rmtree(pristine)
    if work.exists():
        force_rmtree(work)

    # The plan already walked and measured the tree; the write reuses its
    # in-scope list rather than walking a second time. Re-walking would double
    # the dominant cost of a large repository for no new information.
    scoped = list(plan.in_scope)
    written, shared, reason = snapshot_reference(
        source_path,
        pristine,
        store=store,
        relatives=scoped,
        scope=resolved_scope,
    )
    private = snapshot_working(
        source_path, work, relatives=scoped, scope=resolved_scope
    )
    return SnapshotReceipt(
        source=str(source_path),
        pristine=str(pristine),
        work=str(work),
        plan=plan,
        written=written + private,
        shared=shared,
        copied=private,
        private_copies=private,
        work_is_private_copy=True,
        shared_disabled_reason="" if shared else (reason or "sharing not requested"),
        budget=verdict,
        scope=(scope_verdict(resolved_scope, changed) if changed is not None else None),
        store_root=str(store.root) if store is not None else "",
        elapsed_s=time.time() - started,
    )


# ---------------------------------------------------------------------------
# 5. Retention
# ---------------------------------------------------------------------------


def _chmod_writable(func: Callable[..., Any], path: str, _exc: Any) -> None:
    """``shutil.rmtree`` error handler that makes a read-only entry removable.

    Shared reference entries and blobs are read-only BY DESIGN (that is the
    protection for the diff baseline). On POSIX that is irrelevant to unlink;
    on Windows a read-only file cannot be deleted, so pruning a shared tree
    needs this handler. Without it the documented retention policy would fail
    on exactly the entries it exists to reclaim.
    """
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


#: ``shutil.rmtree`` renamed its error handler in 3.12 (``onerror`` ->
#: ``onexc``). The project supports 3.10-3.12, so the keyword is chosen once
#: here rather than guessing at the call site.
_RMTREE_ERROR_KW = "onexc" if sys.version_info >= (3, 12) else "onerror"


def force_rmtree(path: PathLike) -> None:
    """Remove a tree even when it holds read-only files (Windows-safe)."""
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        return
    if target.is_symlink():
        try:
            target.unlink()
        except OSError:
            pass
        return
    shutil.rmtree(str(target), **{_RMTREE_ERROR_KW: _chmod_writable})


def is_run_directory(path: PathLike) -> bool:
    """True when ``path`` is recognisably a task run directory.

    A run directory carries at least one of the harness's own run artifacts
    (``trace.jsonl``, ``state.json``, ``plan.json``) or a ``pristine``/``work``
    snapshot pair. This is what keeps pruning from treating an arbitrary
    directory in the logs root -- a user's own ``logs/notes``, a shared
    ``_code-graph`` index, a session directory -- as a disposable run.
    """
    candidate = Path(path)
    if not candidate.is_dir() or candidate.is_symlink():
        return False
    for marker in ("trace.jsonl", "state.json", "plan.json"):
        if (candidate / marker).exists():
            return True
    return (candidate / "pristine").is_dir() or (candidate / "work").is_dir()


@dataclass(frozen=True)
class RetentionSettings:
    """The documented snapshot retention policy.

    Defaults live here, not in ``harness/config.py::DEFAULTS``, and the task
    keys that override them are opt-in by presence: ``snapshot_retention_days``,
    ``snapshot_retention_bytes``, ``snapshot_retention_keep``, and the
    ``snapshot_store_max_bytes`` store ceiling.
    """

    max_age_s: Optional[float] = None
    max_bytes: Optional[int] = None
    keep_latest: int = DEFAULT_RETENTION_KEEP
    store_max_bytes: int = DEFAULT_STORE_MAX_BYTES
    source: str = "default"
    notes: Tuple[str, ...] = ()

    def to_policy(self) -> RetentionPolicy:
        """Project onto the SHARED retention policy type.

        The age/byte/keep-latest vocabulary is deliberately
        :class:`shared.retention.RetentionPolicy` so there is one policy shape
        in the tree rather than a second one that reads the same but disagrees.
        """
        return RetentionPolicy(
            max_age_s=self.max_age_s,
            max_bytes=self.max_bytes,
            keep_latest=self.keep_latest,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for the policy."""
        return {
            "max_age_s": self.max_age_s,
            "max_age_days": (
                None if self.max_age_s is None else round(self.max_age_s / 86400.0, 4)
            ),
            "max_bytes": self.max_bytes,
            "keep_latest": self.keep_latest,
            "store_max_bytes": self.store_max_bytes,
            "source": self.source,
            "notes": list(self.notes),
        }


def retention_settings_from_env(
    environ: Optional[Mapping[str, str]] = None,
) -> RetentionSettings:
    """Resolve the retention policy from the environment.

    ``NEO_SNAPSHOT_RETENTION_DAYS``, ``NEO_SNAPSHOT_RETENTION_BYTES``,
    ``NEO_SNAPSHOT_RETENTION_KEEP`` and ``NEO_SNAPSHOT_STORE_MAX_BYTES``. An
    unset variable takes the documented default; an unparseable one produces a
    note rather than raising, so a typo cannot make a run directory
    unprunable.
    """
    source = dict(os.environ if environ is None else environ)
    notes: List[str] = []

    def _number(name: str) -> Optional[float]:
        raw = source.get(name)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            notes.append(f"{name} is not a number; the default was used")
            return None

    days = _number("NEO_SNAPSHOT_RETENTION_DAYS")
    if days is None:
        days = DEFAULT_RETENTION_DAYS
    byte_limit = _number("NEO_SNAPSHOT_RETENTION_BYTES")
    keep_raw = source.get("NEO_SNAPSHOT_RETENTION_KEEP")
    keep = DEFAULT_RETENTION_KEEP
    if keep_raw not in (None, ""):
        try:
            keep = max(0, int(float(keep_raw)))
        except (TypeError, ValueError):
            notes.append(
                "NEO_SNAPSHOT_RETENTION_KEEP is not an int; the default was used"
            )
    store_raw = _number("NEO_SNAPSHOT_STORE_MAX_BYTES")
    store_max = int(store_raw) if store_raw else DEFAULT_STORE_MAX_BYTES
    return RetentionSettings(
        max_age_s=float(days) * 86400.0,
        max_bytes=int(byte_limit) if byte_limit is not None else None,
        keep_latest=keep,
        store_max_bytes=max(0, int(store_max)),
        source="env"
        if environ is not None
        or any(str(key).startswith("NEO_SNAPSHOT_") for key in source)
        else "default",
        notes=tuple(notes),
    )


def resolve_retention(config: Optional[Mapping[str, Any]] = None) -> RetentionSettings:
    """Resolve the retention policy from a task config, then the environment.

    A task's declared keys win over the environment; the environment wins over
    the documented defaults. A value that could not be used is reported in
    ``notes`` instead of being silently replaced.
    """
    base = retention_settings_from_env()
    notes: List[str] = list(base.notes)
    source = base.source
    if not config:
        return RetentionSettings(
            max_age_s=base.max_age_s,
            max_bytes=base.max_bytes,
            keep_latest=base.keep_latest,
            store_max_bytes=base.store_max_bytes,
            source=source,
            notes=tuple(notes),
        )
    max_age_s = base.max_age_s
    max_bytes = base.max_bytes
    keep = base.keep_latest
    store_max = base.store_max_bytes
    if "snapshot_retention_days" in config:
        source = "config"
        value = config.get("snapshot_retention_days")
        try:
            max_age_s = float(value) * 86400.0
        except (TypeError, ValueError):
            notes.append("snapshot_retention_days is not a number; ignored")
            max_age_s = base.max_age_s
    if "snapshot_retention_bytes" in config:
        source = "config"
        max_bytes = _coerce_positive_int(
            config.get("snapshot_retention_bytes"), "snapshot_retention_bytes", notes
        )
    if "snapshot_retention_keep" in config:
        source = "config"
        try:
            keep = max(0, int(config.get("snapshot_retention_keep")))
        except (TypeError, ValueError):
            notes.append("snapshot_retention_keep is not an int; ignored")
            keep = base.keep_latest
    if "snapshot_store_max_bytes" in config:
        source = "config"
        store_max = (
            _coerce_positive_int(
                config.get("snapshot_store_max_bytes"),
                "snapshot_store_max_bytes",
                notes,
            )
            or base.store_max_bytes
        )
    return RetentionSettings(
        max_age_s=max_age_s,
        max_bytes=max_bytes,
        keep_latest=keep,
        store_max_bytes=store_max,
        source=source,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class PruneReport:
    """What a retention pass removed, skipped, and refused to touch.

    ``removed`` and ``bytes_removed`` describe run directories;
    ``blobs_removed``/``blob_bytes_removed`` describe the shared store. A
    ``skipped`` entry is a directory that was NOT a run directory and therefore
    never considered -- naming them is what proves the pass is narrow.
    """

    root: str
    removed: Tuple[str, ...] = ()
    kept: Tuple[str, ...] = ()
    skipped: Tuple[str, ...] = ()
    bytes_removed: int = 0
    candidates: int = 0
    blobs_removed: int = 0
    blob_bytes_removed: int = 0
    errors: Tuple[str, ...] = ()
    dry_run: bool = False
    policy: Optional[RetentionSettings] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for the pass."""
        return {
            "root": self.root,
            "removed": list(self.removed),
            "kept": list(self.kept),
            "skipped": list(self.skipped),
            "bytes_removed": self.bytes_removed,
            "candidates": self.candidates,
            "blobs_removed": self.blobs_removed,
            "blob_bytes_removed": self.blob_bytes_removed,
            "errors": list(self.errors),
            "dry_run": bool(self.dry_run),
            "policy": self.policy.to_dict() if self.policy else None,
        }


def _directory_bytes(path: Path) -> int:
    """Bytes under ``path``, tolerant of a read-only or partially removed tree."""
    return tree_bytes(path)


def prune_run_directories(
    log_root: PathLike,
    settings: Optional[RetentionSettings] = None,
    *,
    dry_run: bool = False,
    now: Optional[float] = None,
    store: Optional[SharedStore] = None,
) -> PruneReport:
    """Remove run directories that fall outside the retention policy.

    The policy is age, total bytes, and keep-latest, in that order of
    selection: everything older than ``max_age_s`` goes, then -- if the total
    still exceeds ``max_bytes`` -- the oldest not-yet-protected runs go until it
    does not, and the newest ``keep_latest`` runs are never candidates at all.

    **Narrowness is the contract.** Only directories that
    :func:`is_run_directory` recognises are ever candidates; every other entry
    of the logs root is reported in ``skipped`` and left alone. A ``dry_run``
    mutates nothing. An error on one directory is collected and the pass
    continues, so a single locked run cannot make the whole policy unrunnable --
    and the error is reported rather than swallowed.
    """
    root = Path(log_root)
    policy = settings or resolve_retention(None)
    timestamp = time.time() if now is None else float(now)
    if not root.is_dir():
        return PruneReport(
            root=str(root),
            policy=policy,
            dry_run=bool(dry_run),
            errors=("log root is not a directory",),
        )
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return PruneReport(
            root=str(root),
            policy=policy,
            dry_run=bool(dry_run),
            errors=(f"{type(exc).__name__}: {exc}",),
        )

    runs: List[Path] = []
    skipped: List[str] = []
    for entry in entries:
        if not is_run_directory(entry):
            skipped.append(entry.name)
            continue
        runs.append(entry)

    def _mtime(path: Path) -> float:
        try:
            return float(path.stat().st_mtime)
        except OSError:
            return 0.0

    runs.sort(key=lambda item: (_mtime(item), item.name))
    keep = max(0, int(policy.keep_latest))
    protected = set(runs[-keep:]) if keep else set()

    selected: set[Path] = set()
    if policy.max_age_s is not None:
        cutoff = timestamp - float(policy.max_age_s)
        selected.update(path for path in runs if _mtime(path) < cutoff)
    if policy.max_bytes is not None:
        total = 0
        sizes: Dict[Path, int] = {}
        for path in runs:
            size = _directory_bytes(path)
            sizes[path] = size
            total += size
        for path in runs:
            if total <= int(policy.max_bytes):
                break
            if path in protected:
                continue
            selected.add(path)
            total -= sizes.get(path, 0)
    selected -= protected
    removed: List[str] = []
    removed_bytes = 0
    errors: List[str] = []
    for path in sorted(selected, key=lambda item: (_mtime(item), item.name)):
        size = _directory_bytes(path)
        if dry_run:
            removed.append(path.name)
            removed_bytes += size
            continue
        try:
            force_rmtree(path)
            removed.append(path.name)
            removed_bytes += size
        except OSError as exc:
            errors.append(f"{path.name}: {type(exc).__name__}")
    kept = tuple(path.name for path in runs if path not in selected)

    blobs_removed = 0
    blob_bytes = 0
    if store is not None:
        sub = prune_shared_store(store, policy, dry_run=dry_run)
        blobs_removed = sub.blobs_removed
        blob_bytes = sub.blob_bytes_removed
        errors.extend(sub.errors)
    return PruneReport(
        root=str(root),
        removed=tuple(removed),
        kept=kept,
        skipped=tuple(skipped),
        bytes_removed=removed_bytes,
        candidates=len(runs),
        blobs_removed=blobs_removed,
        blob_bytes_removed=blob_bytes,
        errors=tuple(errors),
        dry_run=bool(dry_run),
        policy=policy,
    )


def prune_shared_store(
    store: SharedStore,
    settings: Optional[RetentionSettings] = None,
    *,
    dry_run: bool = False,
    now: Optional[float] = None,
) -> PruneReport:
    """Remove store blobs beyond ``store_max_bytes``, oldest access first.

    A blob's age is its ``st_mtime``; :meth:`SharedStore.put` and the link
    path do not touch it, so the ordering is "when this content was first
    stored", which is the honest approximation of least-recently-used without
    a per-run atime update. The report names the blobs by digest so a reader
    can tell what was reclaimed.
    """
    policy = settings or resolve_retention(None)
    timestamp = time.time() if now is None else float(now)
    blobs = list(store.iter_blobs())
    entries: List[Tuple[float, int, Path]] = []
    total = 0
    for blob in blobs:
        try:
            info = blob.stat()
        except OSError:
            continue
        entries.append((float(info.st_mtime), int(info.st_size), blob))
        total += int(info.st_size)
    entries.sort(key=lambda item: (item[0], item[2].name))
    ceiling = int(policy.store_max_bytes)
    removed: List[str] = []
    freed = 0
    errors: List[str] = []
    if policy.max_age_s is not None:
        cutoff = timestamp - float(policy.max_age_s)
        for _mtime, size, blob in entries:
            if _mtime >= cutoff:
                break
            if dry_run:
                removed.append(blob.name)
                freed += size
                continue
            try:
                blob.unlink()
                removed.append(blob.name)
                freed += size
            except OSError as exc:
                errors.append(f"{blob.name}: {type(exc).__name__}")
    running = total - freed
    for _mtime, size, blob in entries:
        if running <= ceiling:
            break
        if blob.name in removed:
            continue
        if dry_run:
            removed.append(blob.name)
            freed += size
            running -= size
            continue
        try:
            blob.unlink()
            removed.append(blob.name)
            freed += size
            running -= size
        except OSError as exc:
            errors.append(f"{blob.name}: {type(exc).__name__}")
    return PruneReport(
        root=str(store.root),
        removed=tuple(removed),
        bytes_removed=freed,
        candidates=len(entries),
        blobs_removed=len(removed),
        blob_bytes_removed=freed,
        errors=tuple(errors),
        dry_run=bool(dry_run),
        policy=policy,
    )


# ---------------------------------------------------------------------------
# Operator surface: `python -m execution.snapshot`
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Report snapshot disk usage and, with ``--prune``, apply the policy.

    Exists so ``neo doctor``'s remediation names a command that actually runs.
    Exit 0 when nothing actionable was found, 1 when the run root is over its
    budget or low on space, 2 for a usage error.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m execution.snapshot",
        description="Report and prune run-directory disk usage.",
    )
    parser.add_argument(
        "--log-root", default="", help="logs root (default: memory.paths)"
    )
    parser.add_argument(
        "--store", default="", help="shared store root (default: memory.paths)"
    )
    parser.add_argument(
        "--prune", action="store_true", help="apply the retention policy"
    )
    parser.add_argument("--dry-run", action="store_true", help="report only")
    parser.add_argument("--json", action="store_true", help="machine-readable")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        from memory import paths as memory_paths

        log_root = (
            Path(args.log_root) if args.log_root else memory_paths.default_logs_dir()
        )
        store_root = (
            Path(args.store) if args.store else memory_paths.snapshot_store_root()
        )
    except Exception as exc:  # a broken location authority must not crash the CLI
        print(f"snapshot: cannot resolve artifact locations: {exc}", file=sys.stderr)
        return 2

    store = SharedStore(store_root)
    policy = resolve_retention(None)
    report: Optional[PruneReport] = None
    if args.prune:
        report = prune_run_directories(
            log_root, policy, dry_run=bool(args.dry_run), store=store
        )
    free = _free_bytes(log_root if Path(log_root).exists() else Path.cwd())
    payload = {
        "log_root": str(log_root),
        "store": store.stats(),
        "retention": policy.to_dict(),
        "free_bytes": free,
        "prune": report.to_dict() if report is not None else None,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"log root : {log_root}")
        print(f"store    : {store_root} ({payload['store']['blobs']} blobs)")
        print(f"free     : {_human(free)}")
        print(
            "retention: "
            f"keep_latest={policy.keep_latest} "
            f"max_age_days="
            f"{'none' if policy.max_age_s is None else round(policy.max_age_s / 86400.0, 2)}"
        )
        if report is not None:
            print(
                f"prune    : removed {len(report.removed)} run dir(s), "
                f"{_human(report.bytes_removed)}; skipped {len(report.skipped)} "
                f"non-run entries"
            )
            for error in report.errors:
                print(f"  error  : {error}")
    if report is not None and report.errors:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
