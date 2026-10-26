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

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
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
    "EMPTY_LINEAGE_VIEW",
    "LINEAGE_DIRNAME",
    "LINEAGE_SCHEMA",
    "LINEAGE_SIDECAR_NAME",
    "LIVE_SESSIONS_NAME",
    "PRUNED_LEDGER_NAME",
    "PRUNED_UNATTRIBUTED_LINEAGE",
    "SCOPE_CONFIG_KEY",
    "SNAPSHOT_BUDGET_CONFIG_KEYS",
    "SNAPSHOT_STATUSES",
    "BudgetSettings",
    "BudgetVerdict",
    "IgnoreRules",
    "LineageRef",
    "LineageView",
    "PruneReport",
    "PrunedLineage",
    "RetentionSettings",
    "SharedStore",
    "SnapshotAvailability",
    "SnapshotBudgetExceeded",
    "SnapshotCommitIncomplete",
    "SnapshotError",
    "SnapshotPlan",
    "SnapshotReceipt",
    "SnapshotScopeError",
    "TaskScope",
    "append_pruned_ledger",
    "content_digest",
    "create_run_snapshot",
    "force_rmtree",
    "git_ignored_paths",
    "is_run_directory",
    "lineage_view",
    "plan_run_snapshot",
    "prune_run_directories",
    "prune_shared_store",
    "pruned_lineages",
    "read_lineage",
    "read_live_sessions",
    "read_pruned_ledger",
    "record_lineage",
    "resolve_budget",
    "resolve_retention",
    "retention_settings_from_env",
    "scope_verdict",
    "snapshot_availability",
    "snapshot_reference",
    "snapshot_working",
    "tree_bytes",
    "write_live_sessions",
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


#: Prefix of the directory a snapshot is built in before it is moved into place.
#: It starts with a dot so it is invisible to a plain directory listing, and it
#: is NOT a run directory (see :func:`is_run_directory`), so a crash that leaves
#: one behind is reported as ``skipped`` by a retention pass rather than pruned
#: or mistaken for a run.
STAGING_PREFIX = ".snapshot-staging-"

#: Sub-directories of the staging directory the PREVIOUS pair is parked in while
#: the new one is committed. A rename within one directory is atomic and copies
#: nothing, so parking is free and cannot itself fail halfway.
RETIRED_PREFIX = ".retired-"


class SnapshotCommitIncomplete(OSError):
    """The staging copy failed AND the previous pair could not be put back.

    Raised only in the one state this module cannot leave quietly: a commit that
    failed mid-rename *and* whose rollback also failed, so ``run_path`` is
    carrying a partial ``pristine``/``work`` pair. A caller that gets this must
    treat the run directory as unrewindable rather than assume the old snapshot
    survived.

    **This is not the same as a failed commit that rolled back cleanly** -- that
    case re-raises the ORIGINAL ``OSError``, because the original cause is the
    one worth reading and the rollback succeeded. The distinction matters: one
    exception means "your previous snapshot is intact", this one means "it is
    not, and you should look".
    """

    def __init__(self, run_path: PathLike, unrecovered: Sequence[str], cause: BaseException):
        self.run_path = str(run_path)
        self.unrecovered = tuple(unrecovered)
        self.cause = cause
        super().__init__(
            f"the snapshot commit failed and {len(self.unrecovered)} of the previous "
            f"snapshot half/halves could not be restored in {run_path}: "
            f"{list(self.unrecovered)}. The run directory is now INCOMPLETE and this "
            f"run is not rewindable; the original failure was "
            f"{type(cause).__name__}: {cause}"
        )


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

    **The write is STAGED, so a failed snapshot leaves NO partial state.**
    Both halves are built inside ``run_dir/.snapshot-staging-<pid>-<uuid>/`` and
    only moved into place once BOTH are complete; any failure removes the staging
    directory and re-raises. Measured before this (2026-10-04): a fault raised
    after 4 of 12 file copies left ``run/pristine/`` holding 3 files, **no**
    ``work/``, and :func:`snapshot_availability` reporting the half-populated
    directory as ``available=True status="present"``. A session that then tried to
    rewind to it got a partial baseline presented as a complete one. The staging
    directory is removed on the failure path and is never a run directory
    (:func:`is_run_directory` will not recognise it), so a crash cannot leave
    something retention would treat as a run.

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
    staging = run_path / f"{STAGING_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}"

    # The plan already walked and measured the tree; the write reuses its
    # in-scope list rather than walking a second time. Re-walking would double
    # the dominant cost of a large repository for no new information.
    scoped = list(plan.in_scope)
    staged_pristine = staging / "pristine"
    staged_work = staging / "work"
    retired_pristine = staging / f"{RETIRED_PREFIX}pristine"
    retired_work = staging / f"{RETIRED_PREFIX}work"
    # (parked, target) for every half whose PREVIOUS version has been moved
    # aside, and (target) for every NEW half already moved into place. Both lists
    # are appended to only AFTER the corresponding rename succeeds, so each entry
    # is a fact rather than an intention -- which is what makes the rollback
    # below able to say exactly what it has to undo.
    parked: List[Tuple[Path, Path]] = []
    landed: List[Path] = []
    try:
        written, shared, reason = snapshot_reference(
            source_path,
            staged_pristine,
            store=store,
            relatives=scoped,
            scope=resolved_scope,
        )
        private = snapshot_working(
            source_path, staged_work, relatives=scoped, scope=resolved_scope
        )
        # Both halves are complete on disk. Commit them.
        #
        # **The commit is TWO renames, and a failure between them is the case this
        # block exists for.** Deleting the old pair first -- the historical shape,
        # and the reason a rename failure could leave a run with a new `pristine`
        # and no `work` at all -- is avoided by PARKING the old halves inside the
        # staging directory first. A rename within one directory is atomic and
        # copies nothing, so parking costs no bytes and cannot tear; and because
        # the old halves are still there under new names, every failure below --
        # including one during the parking itself -- has something to put back.
        # `os.replace` onto an existing non-empty directory fails on Windows, which
        # is why the old halves are renamed away rather than overwritten in place.
        for target, retire in ((pristine, retired_pristine), (work, retired_work)):
            if target.exists():
                os.replace(str(target), str(retire))
                parked.append((retire, target))
        for staged, target in ((staged_pristine, pristine), (staged_work, work)):
            os.replace(str(staged), str(target))
            landed.append(target)
    except BaseException as commit_error:
        unrecovered: List[str] = []
        # Drop the new halves that landed, then put the previous pair back.
        # Removing a landed half first is load-bearing: restoring ON TOP of a
        # partial new half would leave a directory whose contents mix two
        # snapshots, which is worse than either of them.
        for target in landed:
            with contextlib.suppress(OSError):
                force_rmtree(target)
        for retire, target in reversed(parked):
            try:
                os.replace(str(retire), str(target))
            except OSError:
                unrecovered.append(target.name)
        with contextlib.suppress(OSError):
            force_rmtree(staging)
        if unrecovered:
            raise SnapshotCommitIncomplete(run_path, unrecovered, commit_error) from (
                commit_error
            )
        # The rollback worked, so the ORIGINAL failure is the one the caller must
        # read; a "your snapshot is fine" wrapper on top of it would only hide the
        # cause.
        raise
    for retire in (retired_pristine, retired_work):
        with contextlib.suppress(OSError):
            force_rmtree(retire)
    with contextlib.suppress(OSError):
        staging.rmdir()

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


#: Directory, under the logs root, holding the lineage sidecar files. It is
#: deliberately NOT a run directory (``is_run_directory`` will not recognise it),
#: so a retention pass can never sweep the lineage record away while leaving the
#: runs it protects -- exactly the failure this exists to prevent.
LINEAGE_DIRNAME = "_lineage"

#: The file inside a RUN directory naming the lineage that run belongs to.
LINEAGE_SIDECAR_NAME = "lineage.json"

#: The append-only ledger of pruned runs. A pruned snapshot must be REPORTED
#: (``available: false`` + a reason), never silently absent, and a run directory
#: that has been deleted cannot carry its own tombstone -- so the record of its
#: removal has to live somewhere the deletion does not reach.
PRUNED_LEDGER_NAME = "pruned.jsonl"

#: The file naming which sessions are live. T3 owns the session tree and writes
#: this; this module only READS it, because "which session is live" is a
#: conversation-level fact and a storage module that invented one would be a
#: second authority for it.
LIVE_SESSIONS_NAME = "live_sessions.json"

LINEAGE_SCHEMA = 1


@dataclass(frozen=True)
class LineageRef:
    """Which lineage a run's snapshots belong to, and who is in it.

    A run that compacts and rotates does not stop needing its snapshots -- the
    CHILD session cannot rewind without the parent's pre-images -- so retention
    has to follow the lineage rather than a flat per-run age rule.

    ``sessions`` is the membership list and ``live_sessions`` the subset that is
    currently open. ``live`` is a PROPERTY of the two, never a declared field:
    a caller that supplied its own ``live`` could author the permission to be
    pruned.
    """

    lineage_id: str
    sessions: Tuple[str, ...] = ()
    live_sessions: Tuple[str, ...] = ()
    parent_session: str = ""
    head_session: str = ""
    recorded_at: Optional[float] = None
    notes: Tuple[str, ...] = ()

    @property
    def live(self) -> bool:
        """True when at least one session in this lineage is live.

        **The whole retention rule in one property.** A lineage's snapshots are
        retained while ANY of its sessions is live, which is why a rotation --
        which ends the parent's session -- must carry the lineage id forward to
        the child rather than starting a new one.
        """
        return bool(self.live_sessions)

    @property
    def session_count(self) -> int:
        """How many sessions have ever belonged to this lineage."""
        return len(self.sessions)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "schema_version": LINEAGE_SCHEMA,
            "lineage_id": self.lineage_id,
            "sessions": list(self.sessions),
            "live_sessions": list(self.live_sessions),
            "parent_session": self.parent_session,
            "head_session": self.head_session,
            "live": self.live,
            "recorded_at": self.recorded_at,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Optional["LineageRef"]:
        """Build a :class:`LineageRef` from a mapping, or ``None`` when unusable.

        Total by design: a corrupt sidecar must not raise out of a retention
        pass. An unparseable record yields ``None`` and the caller treats the run
        as **lineage-unknown**, which is the refusing direction -- see
        :func:`lineage_view`.
        """
        try:
            lineage_id = str(value.get("lineage_id", "") or "").strip()
            if not lineage_id:
                return None
            if value.get("schema_version") not in (None, LINEAGE_SCHEMA):
                return None
            sessions = tuple(str(item) for item in (value.get("sessions") or ()))
            live = tuple(str(item) for item in (value.get("live_sessions") or ()))
            recorded = value.get("recorded_at")
            return cls(
                lineage_id=lineage_id,
                sessions=sessions,
                live_sessions=live,
                parent_session=str(value.get("parent_session", "") or ""),
                head_session=str(value.get("head_session", "") or ""),
                recorded_at=None if recorded is None else float(recorded),
                notes=tuple(str(item) for item in (value.get("notes") or ())),
            )
        except (TypeError, ValueError, AttributeError):
            return None


@dataclass(frozen=True)
class LineageView:
    """The lineage facts a retention pass needs, resolved ONCE per pass.

    ``live`` maps a lineage id to whether it is live. ``session_to_lineage`` maps
    a session id to its lineage, which is what makes a run that never wrote a
    sidecar still attributable: the run's OWN session id is enough to place it.

    ``unresolved_sessions`` names sessions the view could not place. A run whose
    session is unresolved is **not silently treated as unlineaged** -- see
    :func:`lineage_view` for why that is the refusing direction.
    """

    live: Mapping[str, bool] = field(default_factory=dict)
    session_to_lineage: Mapping[str, str] = field(default_factory=dict)
    live_sessions: Tuple[str, ...] = ()
    unresolved_sessions: Tuple[str, ...] = ()
    source: str = "none"
    notes: Tuple[str, ...] = ()

    def lineage_of(self, session_id: str) -> str:
        """The lineage a session belongs to, or ``""`` when unknown."""
        return str(self.session_to_lineage.get(str(session_id or ""), ""))

    def is_live(self, lineage_id: str) -> bool:
        """True when the named lineage has at least one live session."""
        if not lineage_id:
            return False
        return bool(self.live.get(str(lineage_id), False))

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for the view."""
        return {
            "source": self.source,
            "live": dict(self.live),
            "session_to_lineage": dict(self.session_to_lineage),
            "live_sessions": list(self.live_sessions),
            "unresolved_sessions": list(self.unresolved_sessions),
            "notes": list(self.notes),
        }


#: A view with nothing in it. Used when a caller supplies no lineage facts, and
#: the one that makes "no lineage information" distinguishable from "every
#: lineage is dead" -- the first is UNKNOWN and the second is a measurement.
EMPTY_LINEAGE_VIEW = LineageView(source="empty")


def _lineage_dir(log_root: PathLike) -> Path:
    """The lineage sidecar directory for a logs root."""
    return Path(log_root) / LINEAGE_DIRNAME


def record_lineage(
    run_dir: PathLike,
    lineage: LineageRef,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[Path]:
    """Record which lineage a run's snapshots belong to; return the sidecar path.

    Written INSIDE the run directory as ``lineage.json`` (a temp file plus
    ``os.replace``, so a crash mid-write cannot leave a half-record that a later
    pass reads as "unlineaged"). ``config`` is accepted and ignored for shape
    compatibility with the other ``*_from_config`` resolvers in this module: the
    lineage identity is a fact the CALLER knows, not something derivable from a
    task config, and inferring it would be false precision.

    Returns ``None`` when the run directory does not exist -- a snapshot that was
    never created has no lineage to record, and inventing one would put a
    sidecar somewhere the run is not.
    """
    if not lineage.lineage_id:
        raise SnapshotError("a lineage must be named before it can be recorded")
    run_path = Path(run_dir)
    if not run_path.is_dir():
        return None
    target = run_path / LINEAGE_SIDECAR_NAME
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    payload = json.dumps(lineage.to_dict(), indent=2, sort_keys=True) + "\n"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return None
    return target


def read_lineage(run_dir: PathLike) -> Optional[LineageRef]:
    """Read a run directory's lineage sidecar. Total: never raises.

    ``None`` means one of three genuinely different things -- there is no
    sidecar, it is unparseable, or it names no lineage -- and
    :func:`prune_run_directories` reports them through distinct reasons rather
    than collapsing them, because "this run predates lineage recording" and "this
    run's lineage record is corrupt" call for different operator responses.
    """
    target = Path(run_dir) / LINEAGE_SIDECAR_NAME
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    return LineageRef.from_dict(value)


def read_live_sessions(log_root: PathLike) -> Dict[str, Any]:
    """Read T3's live-session declaration. Total: never raises, never guesses.

    The file is ``<log_root>/_lineage/live_sessions.json`` and its shape is::

        {"schema_version": 1,
         "sessions": [{"session_id": "sess-ab12", "lineage_id": "lin-1"}, ...]}

    An absent file, an unreadable file, a wrong schema, or a session row missing
    its id all yield ``{}`` plus a ``reason``. **An unreadable liveness
    declaration is NOT "every session is dead"** -- that reading would prune every
    live lineage's snapshots on the strength of a file nobody could parse, which
    is precisely the data loss this layer exists to prevent.
    """
    path = _lineage_dir(log_root) / LIVE_SESSIONS_NAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"sessions": [], "reason": "no live-session declaration exists yet"}
    except OSError as exc:
        return {
            "sessions": [],
            "reason": f"the live-session declaration could not be read ({type(exc).__name__})",
        }
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {"sessions": [], "reason": "the live-session declaration is not valid JSON"}
    if not isinstance(value, dict):
        return {"sessions": [], "reason": "the live-session declaration is not a mapping"}
    version = value.get("schema_version")
    if version != LINEAGE_SCHEMA:
        return {
            "sessions": [],
            "reason": (
                f"the live-session declaration carries schema_version {version!r} "
                f"rather than {LINEAGE_SCHEMA!r}"
            ),
        }
    rows = value.get("sessions")
    if not isinstance(rows, list):
        return {"sessions": [], "reason": "the live-session declaration has no session list"}
    sessions: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        session_id = str(row.get("session_id", "") or "").strip()
        if not session_id:
            continue
        sessions.append({"session_id": session_id, "lineage_id": str(row.get("lineage_id", "") or "")})
    return {"sessions": sessions, "reason": ""}


def write_live_sessions(log_root: PathLike, sessions: Iterable[Mapping[str, Any]]) -> bool:
    """Write the live-session declaration. This exists for T3 and for tests.

    **This module does not CALL it.** "Which session is live" is a
    conversation-level fact; T3 owns the session tree and therefore owns this
    fact. The writer lives here only so the shape has exactly one definition --
    a reader and a writer that spell the file differently is two formats -- and
    a pin asserts that :func:`read_live_sessions` round-trips this writer.
    """
    rows: List[Dict[str, str]] = []
    for row in sessions or ():
        session_id = str(dict(row).get("session_id", "") or "").strip()
        if not session_id:
            continue
        rows.append(
            {
                "session_id": session_id,
                "lineage_id": str(dict(row).get("lineage_id", "") or ""),
            }
        )
    directory = _lineage_dir(log_root)
    target = directory / LIVE_SESSIONS_NAME
    payload = (
        json.dumps({"schema_version": LINEAGE_SCHEMA, "sessions": rows}, indent=2, sort_keys=True)
        + "\n"
    )
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False
    return True


def lineage_view(
    log_root: PathLike,
    *,
    live: Optional[Mapping[str, Any]] = None,
) -> LineageView:
    """Resolve the lineage facts a retention pass needs, once.

    ``live`` overrides the on-disk declaration, which is what lets T3 hand in
    the tree's own truth and lets a test hand in a fixture without a file.

    **There are THREE states, not two, and the difference is load-bearing.**

    ``declaration`` / ``caller`` -- the facts were read. A run whose lineage is
    live is protected; everything else, INCLUDING a run with no sidecar at all,
    is prunable. That is the R2-10 retention contract unchanged, plus lineage
    protection.

    ``absent`` -- **no declaration file exists.** No lineage information has ever
    been written for this tree, which is the normal state before this feature
    existed and on a fresh install. The R2-10 contract applies unchanged: an
    unplaced run is prunable. Treating "absent" as "unknown" would make
    retention permanently inert on every tree that has not adopted lineage
    recording, which is a disk leak disguised as a safety mechanism.

    ``unreadable`` -- a declaration EXISTS and could not be understood (corrupt
    body, wrong schema). Here the facts are genuinely unknown while a writer
    believes it published them, so :func:`prune_run_directories` refuses to
    prune anything it cannot place. Pruning on the strength of a file nobody
    could parse is coin-flip data loss.

    Every pass publishes which of the three it was, so a reader never has to
    guess whether lineage protection was available, working, or broken.
    """
    notes: List[str] = []
    rows: List[Dict[str, str]] = []
    if live is not None:
        source = "caller"
        candidate = live.get("sessions") if isinstance(live, Mapping) else live
        for row in candidate or ():  # type: ignore[union-attr]
            if isinstance(row, Mapping):
                session_id = str(row.get("session_id", "") or "").strip()
                if session_id:
                    rows.append(
                        {
                            "session_id": session_id,
                            "lineage_id": str(row.get("lineage_id", "") or ""),
                        }
                    )
        notes.append("lineage facts came from the caller, not from disk")
    else:
        declared = read_live_sessions(log_root)
        rows = list(declared.get("sessions") or [])
        reason = str(declared.get("reason") or "")
        if reason:
            # `read_live_sessions` distinguishes "no file" from "broken file";
            # that distinction decides whether the pass may prune at all.
            source = "absent" if reason.startswith("no live-session declaration exists") \
                else "unreadable"
            notes.append(reason)
        else:
            source = "declaration"

    session_to_lineage: Dict[str, str] = {}
    live_lineages: Dict[str, bool] = {}
    unresolved: List[str] = []
    for row in rows:
        session_id = row["session_id"]
        lineage_id = row.get("lineage_id", "")
        if not lineage_id:
            unresolved.append(session_id)
            continue
        session_to_lineage[session_id] = lineage_id
        live_lineages[lineage_id] = True
    if unresolved:
        notes.append(
            "these live sessions name no lineage and cannot protect anything: "
            + ", ".join(sorted(unresolved))
        )
    return LineageView(
        live=live_lineages,
        session_to_lineage=session_to_lineage,
        live_sessions=tuple(row["session_id"] for row in rows),
        unresolved_sessions=tuple(sorted(unresolved)),
        source=source,
        notes=tuple(notes),
    )


def append_pruned_ledger(
    log_root: PathLike,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    """Append rows to the pruned-run ledger; return how many landed.

    A run directory that has been deleted cannot carry its own tombstone, so the
    record that it USED to exist lives beside the logs. This is what lets
    :func:`snapshot_availability` answer "is there a snapshot here?" with
    ``available: false`` and a reason instead of an indistinguishable absence.
    """
    materialised = [dict(row) for row in rows or ()]
    if not materialised:
        return 0
    directory = _lineage_dir(log_root)
    target = directory / PRUNED_LEDGER_NAME
    written = 0
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            for row in materialised:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                written += 1
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        return written
    return written


def read_pruned_ledger(log_root: PathLike) -> Dict[str, Dict[str, Any]]:
    """Read the pruned-run ledger as ``{run_name: row}``. Total: never raises.

    A torn or unparseable line is skipped rather than failing the read, which is
    the same tolerance :func:`_read_jsonl` applies elsewhere in the tree: a
    half-written final line is a crash artefact, not corruption.
    """
    target = _lineage_dir(log_root) / PRUNED_LEDGER_NAME
    found: Dict[str, Dict[str, Any]] = {}
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return found
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        name = str(row.get("run", "") or "")
        if name:
            found[name] = row
    return found


@dataclass(frozen=True)
class SnapshotAvailability:
    """Whether a run's snapshots are here, with the reason when they are not.

    **This is the ``available: false`` + reason shape, and the reason it exists
    is that the absence must be ATTRIBUTABLE.** A consumer reading ``0`` files
    concludes the run had no snapshots; this type distinguishes:

    * ``present`` -- BOTH halves of the ``pristine``/``work`` pair are on disk;
    * ``incomplete`` -- the run directory exists and carries exactly ONE half of
      the pair. A rewind needs both: ``pristine`` is the restore source and
      ``work`` is the diff baseline, so a half pair restores part of a turn and
      reports nothing about the rest. Measured before this status existed
      (2026-10-04): a snapshot write that failed after 4 of 12 file copies left
      ``pristine/`` and no ``work/``, and this reader answered
      ``available=True status="present"`` for it. A partial baseline presented
      as a complete one is worse than an honest absence, because a session will
      rewind into it and believe the rewind was exact;
    * ``never_created`` -- no run directory, and no prune record;
    * ``pruned`` -- a retention pass removed it, and the ledger says when, why
      and which lineage;
    * ``unreadable`` -- the directory exists but could not be inspected.

    Every one of the last four is ``available: False``. None of them is ``0``.
    """

    run: str
    available: bool
    reason: str
    status: str = "present"
    lineage_id: str = ""
    pruned_at: Optional[float] = None
    pruned_by: str = ""
    bytes_at_prune: int = 0
    present_sides: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "run": self.run,
            "available": self.available,
            "reason": self.reason,
            "status": self.status,
            "lineage_id": self.lineage_id,
            "pruned_at": self.pruned_at,
            "pruned_by": self.pruned_by,
            "bytes_at_prune": self.bytes_at_prune,
            "present_sides": list(self.present_sides),
        }


#: The closed set of availability statuses. Exported so a consumer can reject an
#: unknown value instead of defaulting to "available", which is the direction
#: that would render a deleted snapshot as a working one.
SNAPSHOT_STATUSES: Tuple[str, ...] = (
    "present",
    "incomplete",
    "never_created",
    "pruned",
    "unreadable",
)


def snapshot_availability(
    run_dir: PathLike,
    *,
    log_root: Optional[PathLike] = None,
) -> SnapshotAvailability:
    """Report whether a run's snapshots are available, and why not when they are not.

    ``log_root`` is where the prune ledger lives; it defaults to the run's own
    parent, which is the layout this module writes. **A missing ledger is not a
    pruned snapshot** -- it yields ``never_created``, because "no record of a
    removal" and "a removal happened" are different claims and only one of them
    is supported by evidence.

    ``present`` requires **both** halves of the pair. ``pristine`` is the restore
    source and ``work`` is the diff baseline, and a reader that accepted one of
    them would hand a session a partial baseline described as a complete one.
    """
    path = Path(run_dir)
    name = path.name
    root = Path(log_root) if log_root is not None else path.parent
    sides = tuple(
        side for side in ("pristine", "work") if (path / side).is_dir()
    )
    lineage = read_lineage(path)
    if len(sides) == 2:
        return SnapshotAvailability(
            run=name,
            available=True,
            reason="the snapshot is on disk",
            status="present",
            lineage_id=lineage.lineage_id if lineage else "",
            present_sides=sides,
        )
    if sides:
        missing = "work" if "pristine" in sides else "pristine"
        return SnapshotAvailability(
            run=name,
            available=False,
            reason=(
                f"{path} carries only {sides[0]}/ of the snapshot pair and has no "
                f"{missing}/, so its file state cannot be rewound exactly: "
                f"{'pristine' if missing == 'work' else 'work'} is the "
                + ("restore source" if missing == "work" else "diff baseline")
                + " a rewind needs. This is what a snapshot write that failed part "
                "way through used to look like, and it is reported as "
                "unavailable rather than as a snapshot that is on disk."
            ),
            status="incomplete",
            lineage_id=lineage.lineage_id if lineage else "",
            present_sides=sides,
        )
    ledger_row = read_pruned_ledger(root).get(name)
    if ledger_row is not None:
        pruned_at = _ledger_float(ledger_row.get("pruned_at"))
        reason = (
            f"a retention pass removed this run's snapshots at "
            f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(pruned_at)) if pruned_at else 'an unrecorded time'}"
            f" because {ledger_row.get('reason', 'no reason was recorded')}"
        )
        lineage_id = str(ledger_row.get("lineage_id", "") or "")
        if lineage_id:
            reason += f"; lineage {lineage_id}"
        return SnapshotAvailability(
            run=name,
            available=False,
            reason=reason,
            status="pruned",
            lineage_id=lineage_id,
            pruned_at=pruned_at,
            pruned_by=str(ledger_row.get("pruned_by", "") or ""),
            bytes_at_prune=int(_ledger_float(ledger_row.get("bytes")) or 0),
        )
    if path.exists():
        return SnapshotAvailability(
            run=name,
            available=False,
            reason=(
                f"{path} exists but carries no pristine/ or work/ snapshot pair, "
                "so its file state cannot be rewound; this run predates snapshot "
                "recording or its snapshot was removed by something other than "
                "the retention pass"
            ),
            status="unreadable",
        )
    return SnapshotAvailability(
        run=name,
        available=False,
        reason=(
            f"no run directory at {path} and no retention-pass record of one being "
            "pruned, so no snapshot was ever taken here"
        ),
        status="never_created",
    )


@dataclass(frozen=True)
class PrunedLineage:
    """A lineage whose runs a retention pass has removed, as ONE named thing.

    **Why this exists.** Wave 2 measured that every per-run surface named a
    lineage by id and nothing named a LINEAGE: a user who looked for their
    session's history after a retention pass found nothing at all, and "nothing"
    and "it was there and was pruned" are different facts. So the prune ledger --
    which already carries one row per removed run with its ``lineage_id`` -- is
    folded per lineage, and a pruned lineage is still NAMED with its run count,
    its bytes, when it was pruned and why.

    ``lineage_id`` is ``""`` for a run that was pruned with no readable record,
    and those are reported under :data:`PRUNED_UNATTRIBUTED_LINEAGE` rather than
    being merged into a real lineage: "we pruned something and do not know whose"
    is a distinct state from any one lineage's history.
    """

    lineage_id: str
    runs: Tuple[str, ...] = ()
    run_count: int = 0
    bytes_pruned: int = 0
    first_pruned_at: Optional[float] = None
    last_pruned_at: Optional[float] = None
    reasons: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt for one lineage's pruned history."""
        return {
            "lineage_id": self.lineage_id,
            "runs": list(self.runs),
            "run_count": self.run_count,
            "bytes_pruned": self.bytes_pruned,
            "first_pruned_at": self.first_pruned_at,
            "last_pruned_at": self.last_pruned_at,
            "reasons": list(self.reasons),
        }

    def describe(self) -> str:
        """One PLAIN sentence naming the lineage, so a surface can render it."""
        when = ""
        if self.last_pruned_at:
            when = (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.last_pruned_at))
            )
        return (
            f"lineage {self.lineage_id or PRUNED_UNATTRIBUTED_LINEAGE}: "
            f"{self.run_count} run(s) pruned, {self.bytes_pruned} B reclaimed"
            + (f", last at {when}" if when else "")
            + "; rewind points for those turns are gone"
        )


#: The label a pruned run with NO readable lineage record is filed under. A named
#: constant because "we pruned something and cannot say whose it was" is a state
#: a user must be able to see, not a lineage with an empty id.
PRUNED_UNATTRIBUTED_LINEAGE = "(lineage not recorded)"


def pruned_lineages(log_root: PathLike) -> Tuple[PrunedLineage, ...]:
    """Fold the prune ledger into one entry per PRUNED LINEAGE.

    Read-only and total. ``lineage_unreadable`` here means the per-run sidecar
    could not be parsed, so the run's lineage is unknown -- the same refusing
    direction retention takes, reported rather than guessed at.
    """
    ledger = read_pruned_ledger(log_root)
    grouped: Dict[str, Dict[str, Any]] = {}
    for run_name in sorted(ledger):
        row = ledger[run_name]
        lineage_id = str(row.get("lineage_id", "") or "")
        bucket = grouped.setdefault(
            lineage_id,
            {"runs": [], "bytes": 0, "first": None, "last": None, "reasons": []},
        )
        bucket["runs"].append(run_name)
        bucket["bytes"] += int(_ledger_float(row.get("bytes")) or 0)
        stamp = _ledger_float(row.get("pruned_at"))
        if stamp is not None:
            bucket["first"] = stamp if bucket["first"] is None else min(bucket["first"], stamp)
            bucket["last"] = stamp if bucket["last"] is None else max(bucket["last"], stamp)
        reason = str(row.get("reason", "") or "")
        if reason and reason not in bucket["reasons"]:
            bucket["reasons"].append(reason)
    return tuple(
        PrunedLineage(
            lineage_id=lineage_id or PRUNED_UNATTRIBUTED_LINEAGE,
            runs=tuple(bucket["runs"]),
            run_count=len(bucket["runs"]),
            bytes_pruned=int(bucket["bytes"]),
            first_pruned_at=bucket["first"],
            last_pruned_at=bucket["last"],
            reasons=tuple(bucket["reasons"]),
        )
        for lineage_id, bucket in sorted(grouped.items())
    )


def _ledger_float(value: Any) -> Optional[float]:
    try:
        if isinstance(value, bool) or value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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


#: What the age window is measured FROM, published on every report.
#:
#: **It is the run directory's mtime, which is its LAST WRITE, not its start.**
#: That was measured, not assumed: writing a run's own sidecar
#: (``record_lineage`` creates ``lineage.json`` inside the run) refreshes the
#: directory mtime, and so does every appended trace row, journal entry and
#: checkpoint of a live run. Two consequences, both stated rather than hidden:
#:
#: * a run that is still being appended to is **never** age-expired. That is the
#:   SAFE direction -- an active run is never deleted out from under itself;
#: * a run that "should" be 40 days old but was written to yesterday is **not**
#:   pruned. Retention is therefore "recently ACTIVE", not "created long ago",
#:   and an operator reading ``max_age_days`` as a creation age would be misled.
#:
#: The alternative -- ageing from the earliest artifact mtime inside the run --
#: costs a walk of every candidate on every pass, which is the cost this policy
#: was written to avoid. The choice is published rather than inherited so a
#: reader does not have to discover it.
AGE_BASIS = "run_directory_mtime_last_write"


@dataclass(frozen=True)
class PruneReport:
    """What a retention pass removed, skipped, and refused to touch.

    ``removed`` and ``bytes_removed`` describe run directories;
    ``blobs_removed``/``blob_bytes_removed`` describe the shared store. A
    ``skipped`` entry is a directory that was NOT a run directory and therefore
    never considered -- naming them is what proves the pass is narrow.

    **The lineage fields are why a pruned run is attributable.** ``removed_rows``
    carries one row per removed run naming its lineage, the reason, the byte
    count and the timestamp, and that row is also appended to the on-disk prune
    ledger -- because a directory that has been deleted cannot carry its own
    tombstone. ``lineage_protected`` names every run a LIVE lineage kept alive;
    a reader can therefore check the pass did not delete what it should not have,
    rather than taking the absence of an error as the evidence.

    ``lineage_view_source`` is the honest label for the pass's knowledge:
    ``declaration`` (T3's live sessions were read), ``caller`` (facts were
    handed in), ``unreadable`` (liveness could not be established, so nothing
    was pruned on the strength of a guess), or ``none``.

    ``lineage_unreadable`` is the FOURTH lineage bucket and it is the one Wave 2
    added. ``lineage_unrecorded`` means "this run has no lineage record", which
    is a real fact about an older run and stays prunable;
    ``lineage_unreadable`` means "this run HAS a record and it cannot be parsed",
    which is a broken fact and is **PROTECTED**. Measured before it existed: a run
    whose ``lineage.json`` was corrupt was pruned and filed under
    ``lineage_unrecorded``, so the module deleted the snapshots of a lineage it
    could not identify *because the record was damaged*.
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
    lineage_protected: Tuple[str, ...] = ()
    lineage_unrecorded: Tuple[str, ...] = ()
    lineage_unreadable: Tuple[str, ...] = ()
    lineage_protected_bytes: int = 0
    lineage_view_source: str = "none"
    removed_rows: Tuple[Mapping[str, Any], ...] = ()

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
            "lineage_protected": list(self.lineage_protected),
            "lineage_unrecorded": list(self.lineage_unrecorded),
            "lineage_unreadable": list(self.lineage_unreadable),
            "lineage_protected_bytes": self.lineage_protected_bytes,
            "lineage_view_source": self.lineage_view_source,
            "age_basis": AGE_BASIS,
            "removed_rows": [dict(row) for row in self.removed_rows],
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
    lineages: Optional[Any] = None,
    session_of_run: Optional[Any] = None,
    max_runs: Optional[int] = None,
) -> PruneReport:
    """Remove run directories that fall outside the retention policy.

    The policy is age, total bytes, and keep-latest, in that order of
    selection: everything older than ``max_age_s`` goes, then -- if the total
    still exceeds ``max_bytes`` -- the oldest not-yet-protected runs go until it
    does not, and the newest ``keep_latest`` runs are never candidates at all.

    **A lineage with a live session is never a candidate, and that is checked
    BEFORE selection rather than after.** ``lineages`` is a :class:`LineageView`
    (or a mapping of ``{lineage_id: live}``, or ``None`` to read T3's declaration
    from ``<log_root>/_lineage/live_sessions.json``). A run is protected when:

    * its own ``lineage.json`` sidecar names a live lineage, OR
    * ``session_of_run`` maps the run to a session id that a live lineage
      contains -- which is what places a run that predates sidecar recording,
      and
    * when ``lineages`` was supplied by the caller.

    **Unrecorded lineage is the refusing direction, and it is reported.** A run
    with no sidecar and no session mapping is NOT treated as unlineaged-and-so-
    prunable; it is counted in ``lineage_unrecorded`` and left alone when the
    view's ``source`` is ``unreadable``. When the source IS a readable
    declaration and a run simply has no sidecar, that is a real fact about an
    older run, so it stays prunable -- but it is named in the receipt, because a
    reader must be able to tell "pruned because it was 40 days old" from "pruned
    because we could not place it".

    **Narrowness is the contract.** Only directories that
    :func:`is_run_directory` recognises are ever candidates; every other entry
    of the logs root is reported in ``skipped`` and left alone. A ``dry_run``
    mutates nothing. An error on one directory is collected and the pass
    continues, so a single locked run cannot make the whole policy unrunnable --
    and the error is reported rather than swallowed.

    **``max_runs`` is the RUN-COUNT bound, and it exists because ``keep_latest``
    is a FLOOR, not a trigger.** Wave 1's policy reads ``keep_latest=N`` as "the
    newest N are never candidates", which on its own can never reclaim anything:
    a root holding 900 fresh runs stays at 900 forever, because nothing is old
    enough to age out. That is the right default (retention should not delete
    this morning's work) and the wrong enforcement. ``max_runs=N`` adds the
    missing half -- **once the root holds more than N run directories, the OLDEST
    ones past the floor become candidates** -- and it is still a floor for a live
    lineage, which is checked before selection exactly as before. Omit it
    (the default) and the pass is byte-identical to Wave 1's.

    This is what :func:`execution.storage_cost.enforce_storage_bound` passes, so
    the published storage bound has exactly one enforcement path.
    """
    root = Path(log_root)
    policy = settings or resolve_retention(None)
    timestamp = time.time() if now is None else float(now)
    if lineages is None:
        view = lineage_view(root)
    elif isinstance(lineages, LineageView):
        view = lineages
    elif isinstance(lineages, Mapping):
        view = lineage_view(root, live=lineages)
    else:
        view = LineageView(source="unreadable", notes=(f"unusable lineage facts: {type(lineages).__name__}",))
    # A CORRUPT declaration means the facts are unknown while a writer believes
    # it published them, so nothing this pass cannot place is pruned. An ABSENT
    # declaration means no lineage information was ever written, which is a
    # normal pre-feature state and must not make retention permanently inert.
    refuse_unplaced = view.source == "unreadable"
    if not root.is_dir():
        return PruneReport(
            root=str(root),
            policy=policy,
            dry_run=bool(dry_run),
            lineage_view_source=view.source,
            errors=("log root is not a directory",),
        )
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return PruneReport(
            root=str(root),
            policy=policy,
            dry_run=bool(dry_run),
            lineage_view_source=view.source,
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

    errors_pre: List[str] = []

    def _sidecar_state(path: Path) -> str:
        """``present`` / ``absent`` / ``unreadable`` for a run's OWN sidecar.

        The three-way distinction, and it is the whole point of this function.
        A run with NO sidecar is a real fact about an older run (Wave 1 §2.2:
        an absent fact is normal and must not make retention permanently inert).
        A run whose sidecar EXISTS and cannot be parsed is a **broken** fact while
        a writer believes it published it -- the refusing direction. Collapsing
        the two into "unrecorded" is what let this round measure a corrupt
        ``lineage.json`` being PRUNED, i.e. the module deleting the snapshots of a
        lineage it could not identify because the record was damaged.
        """
        sidecar = path / LINEAGE_SIDECAR_NAME
        if not sidecar.exists():
            return "absent"
        return "present" if read_lineage(path) is not None else "unreadable"

    def _lineage_of(path: Path) -> str:
        """The lineage a run belongs to, from its sidecar or its session."""
        sidecar = read_lineage(path)
        if sidecar is not None and sidecar.live_sessions:
            # The sidecar's own live list is honoured, and OR-ed with the view:
            # a run that recorded a live session keeps its snapshots even when
            # the central declaration has not caught up yet.
            return sidecar.lineage_id
        if sidecar is not None and sidecar.lineage_id:
            return sidecar.lineage_id
        if session_of_run is not None:
            try:
                session_id = str(session_of_run(path.name) or "")
            except Exception as exc:  # a caller's placement must not crash GC
                errors_pre.append(f"{path.name}: lineage placement failed ({type(exc).__name__})")
                return ""
            if session_id:
                return view.lineage_of(session_id)
        return ""

    protected_lineage: Dict[Path, str] = {}
    unrecorded: List[str] = []
    unreadable_sidecar: List[str] = []
    for path in runs:
        state = _sidecar_state(path)
        if state == "unreadable":
            # A DAMAGED lineage record is protected, and named in its own bucket
            # so a reader can tell "we could not place this run" from "this run
            # has no lineage". Re-reading the sidecar here rather than trusting
            # `read_lineage() is None` alone is what keeps the two apart.
            unreadable_sidecar.append(path.name)
            protected_lineage[path] = ""
            continue
        lineage_id = _lineage_of(path)
        if not lineage_id:
            unrecorded.append(path.name)
            continue
        sidecar = read_lineage(path)
        is_live = view.is_live(lineage_id) or bool(sidecar and sidecar.live)
        if is_live:
            protected_lineage[path] = lineage_id

    runs.sort(key=lambda item: (_mtime(item), item.name))
    keep = max(0, int(policy.keep_latest))
    protected = set(runs[-keep:]) if keep else set()
    protected |= set(protected_lineage)
    if refuse_unplaced:
        protected |= {path for path in runs if path.name in set(unrecorded)}

    selected: set[Path] = set()
    #: The paths selected BY the run-count bound rather than by age/bytes. Kept
    #: apart so the prune ledger says WHICH rule removed each run -- a pruned
    #: lineage whose receipt says "outside the retention window" when it was
    #: actually the run-count ceiling is a receipt that misattributes the loss.
    by_run_count: set[Path] = set()
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
    if max_runs is not None and int(max_runs) >= 0:
        floor = int(max_runs)
        surplus = len(runs) - floor
        for path in runs:  # oldest first, so the surplus is the OLDEST surplus
            if surplus <= 0:
                break
            if path in protected or path in selected:
                continue
            selected.add(path)
            by_run_count.add(path)
            surplus -= 1
    selected -= protected
    by_run_count -= protected
    removed: List[str] = []
    removed_bytes = 0
    removed_rows: List[Dict[str, Any]] = []
    errors: List[str] = list(errors_pre)
    lineage_protected_bytes = sum(_directory_bytes(path) for path in protected_lineage)
    for path in sorted(selected, key=lambda item: (_mtime(item), item.name)):
        size = _directory_bytes(path)
        policy_words = (
            f"(keep_latest={policy.keep_latest}, max_age_days="
            f"{'none' if policy.max_age_s is None else round(policy.max_age_s / 86400.0, 2)}, "
            f"max_bytes={'none' if policy.max_bytes is None else policy.max_bytes})"
        )
        row = {
            "run": path.name,
            "lineage_id": protected_lineage.get(path, "") or _lineage_of(path),
            "reason": (
                f"the logs root held more than max_runs={max_runs} run directories and "
                f"this is one of the oldest of the surplus {policy_words}"
                if path in by_run_count
                else f"it was outside the retention window {policy_words}"
            ),
            "selected_by": "run_count" if path in by_run_count else "age_or_bytes",
            "bytes": size,
            "pruned_at": timestamp,
            "pruned_by": "execution.snapshot.prune_run_directories",
            "dry_run": bool(dry_run),
        }
        if dry_run:
            removed.append(path.name)
            removed_bytes += size
            removed_rows.append(row)
            continue
        try:
            force_rmtree(path)
            removed.append(path.name)
            removed_bytes += size
            removed_rows.append(row)
        except OSError as exc:
            errors.append(f"{path.name}: {type(exc).__name__}")
    kept = tuple(path.name for path in runs if path not in selected)
    if removed_rows and not dry_run:
        append_pruned_ledger(root, removed_rows)

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
        lineage_protected=tuple(
            sorted(path.name for path in protected_lineage)
        ),
        lineage_unrecorded=tuple(sorted(unrecorded)),
        lineage_unreadable=tuple(sorted(unreadable_sidecar)),
        lineage_protected_bytes=lineage_protected_bytes,
        lineage_view_source=view.source,
        removed_rows=tuple(removed_rows),
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
    parser.add_argument(
        "--no-lineage",
        action="store_true",
        help="ignore live lineages (UNSAFE: a live session's snapshots can be pruned)",
    )
    parser.add_argument(
        "--availability",
        default="",
        help="report whether one run's snapshots are available, and why not",
    )
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
    availability: Optional[SnapshotAvailability] = None
    if args.availability:
        candidate = Path(args.availability)
        availability = snapshot_availability(
            candidate if candidate.is_dir() else log_root / args.availability,
            log_root=log_root,
        )
    if args.prune:
        report = prune_run_directories(
            log_root,
            policy,
            dry_run=bool(args.dry_run),
            store=store,
            lineages=EMPTY_LINEAGE_VIEW if args.no_lineage else None,
        )
    free = _free_bytes(log_root if Path(log_root).exists() else Path.cwd())
    payload = {
        "log_root": str(log_root),
        "store": store.stats(),
        "retention": policy.to_dict(),
        "free_bytes": free,
        "prune": report.to_dict() if report is not None else None,
        "availability": availability.to_dict() if availability is not None else None,
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
        if availability is not None:
            print(
                f"snapshot : {availability.run} available="
                f"{availability.available} status={availability.status}"
            )
            print(f"           {availability.reason}")
        if report is not None:
            print(
                f"prune    : removed {len(report.removed)} run dir(s), "
                f"{_human(report.bytes_removed)}; skipped {len(report.skipped)} "
                f"non-run entries"
            )
            print(
                f"lineage  : source={report.lineage_view_source} "
                f"protected={len(report.lineage_protected)} "
                f"({_human(report.lineage_protected_bytes)}) "
                f"unrecorded={len(report.lineage_unrecorded)}"
            )
            for name in report.lineage_protected:
                print(f"  kept   : {name} (a live session in its lineage)")
            for row in report.removed_rows:
                print(
                    f"  removed: {row['run']} ({_human(int(row.get('bytes') or 0))}) "
                    f"lineage={row.get('lineage_id') or 'unrecorded'} - "
                    f"{row.get('reason', '')}"
                )
            for error in report.errors:
                print(f"  error  : {error}")
    if availability is not None and not availability.available:
        return 1
    if report is not None and report.errors:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
