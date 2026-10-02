"""Filesystem location conventions for the harness-owned state roots.

One place decides where harness artifacts live, so the CLI, the MCP server,
the dashboard, and the memory layer agree (and tests can override via env).

Defaults (VEX-CEILING-03 — safe default artifact location):

    NEO_HOME                 (default: the platform Neo data dir)
    HARNESS_HOME             (legacy alias for NEO_HOME; still honoured)
    HARNESS_DECISIONS_DB     (default: $NEO_HOME/memory/decisions.db)
    HARNESS_LOGS_DIR         (default: $NEO_HOME/logs/<repo-key>)

THE IMPORTANT PROPERTY: no harness artifact is written inside the user's
repository unless the user explicitly forces it. A run directory holds a
full ``pristine`` + ``work`` copy of the target source tree, so an in-repo
default silently duplicated the user's repository on every run. The
default log root is now a harness-owned home keyed by a stable repo key,
and a FORCED in-repo root is gitignored plus warned about (see
``ensure_log_root_ignored``).

R2-10 added the SNAPSHOT ARTIFACT roots and the retention receipts. A run
directory is the largest thing the harness writes (~1.2 GB measured for an
unfiltered snapshot pair), so this module also owns WHERE the shared
content store lives, WHICH retention policy applies, and the single
read-only receipt ``cli.doctor`` renders as its disk section. The
mechanics (ignore rules, scope, budget, sharing, pruning) live in
``execution.snapshot``; the import of it below is LAZY and function-local so
``memory.paths`` keeps its position as a location authority that can be
imported by anything, including from inside ``execution`` itself.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "decisions_db_path",
    "default_logs_dir",
    "ensure_log_root_ignored",
    "find_git_root",
    "harness_home",
    "is_safe_task_id",
    "is_within",
    "log_root_placement",
    "neo_home",
    "prune_snapshot_artifacts",
    "repo_key",
    "retention_receipt",
    "safe_state_file",
    "safe_task_dir",
    "snapshot_disk_receipt",
    "snapshot_store_root",
]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG = 32
_REPO_KEY_DIGEST = 10


def _env_path(*names: str) -> Optional[Path]:
    """First non-empty env var from `names`, expanded + resolved."""
    for name in names:
        raw = os.environ.get(name)
        if raw and raw.strip():
            try:
                return Path(raw).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                return Path(raw).expanduser()
    return None


def _data_root() -> Path:
    """The platform data directory the state root lives under.

    Split out of :func:`neo_home` because the legacy-name fallback needs the
    SAME base to look beside the current root; resolving it twice is how two
    paths end up meaning the same directory and disagreeing about it.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) if base else Path.home() / "AppData" / "Local"
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base).expanduser() if base else Path.home() / ".local" / "share"


def _resolve_under(root: Path, name: str) -> Path:
    try:
        return (root / name).resolve()
    except (OSError, RuntimeError, ValueError):
        return root / name


def neo_home() -> Path:
    """Root directory for harness-owned state; never inside a repository.

    Resolution order, and the order is the contract:

    1. ``NEO_HOME`` — the current name, always wins.
    2. ``VEX_HOME`` — the previous product name, still honoured so an
       upgraded install keeps the same root.
    3. ``HARNESS_HOME`` — the name that predates both, still honoured.
    4. the platform data directory: ``%LOCALAPPDATA%\\neo`` on Windows,
       ``$XDG_DATA_HOME/neo`` (else ``~/.local/share/neo``) elsewhere —
       UNLESS a directory under the previous name ``vex`` already exists,
       in which case that one is used so the install's existing settings,
       sessions and logs are not orphaned by a rename.

    Rule 4's legacy branch is a READ fallback and never migrates: it does not
    create, copy, rename or write into the legacy directory. Silently moving
    a user's state is how a tool loses it, and a user who wants the new
    layout can point ``NEO_HOME`` at it themselves.

    Never raises: an unwritable platform location still yields a path the
    caller can attempt, and a real failure surfaces at first write.
    """
    override = _env_path("NEO_HOME", "VEX_HOME", "HARNESS_HOME")
    if override is not None:
        return override
    root = _data_root()
    try:
        from shared.brand import HOME_DIRNAME, LEGACY_HOME_DIRNAME
    except Exception:  # the brand module is optional; the fallback is not
        return _resolve_under(root, "neo")
    current = _resolve_under(root, HOME_DIRNAME)
    legacy = _resolve_under(root, LEGACY_HOME_DIRNAME)
    try:
        if not current.exists() and legacy.is_dir():
            return legacy
    except OSError:
        pass
    return current


def harness_home() -> Path:
    """Root directory for harness-owned state (never committed).

    Historical name kept for every existing caller; the default is now the
    harness-owned home outside the repository rather than ``./.harness``.
    """
    return neo_home()


def decisions_db_path() -> Path:
    """SQLite file backing DecisionStore."""
    env = _env_path("HARNESS_DECISIONS_DB")
    if env is not None:
        return env
    return harness_home() / "memory" / "decisions.db"


def canonical_repo(repo: object = None) -> Path:
    """Best-effort absolute, symlink-resolved repository path.

    Falls back to the process CWD, which is the historical default source
    for every location decision. Never raises.
    """
    candidate: Optional[Path]
    if repo is None or (isinstance(repo, str) and not repo.strip()):
        candidate = Path.cwd()
    else:
        candidate = Path(str(repo))
    try:
        return candidate.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return candidate.absolute()


def repo_key(repo: object = None) -> str:
    """Stable, filesystem-safe key identifying one repository.

    ``<slug>-<10 hex of sha256(canonical path)>``. The slug keeps the
    directory human-readable (``coding-harness-a1b2c3d4e5``); the digest
    keeps two repositories with the same folder name apart, and keeps the
    key stable across renames of anything but the repo root itself.
    ``normcase`` is applied so Windows and POSIX agree for the same path.
    """
    resolved = canonical_repo(repo)
    try:
        canonical = os.path.normcase(str(resolved))
    except (OSError, ValueError):
        canonical = str(resolved)
    digest = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()
    slug = _SLUG_RE.sub("-", resolved.name.lower()).strip("-")[:_MAX_SLUG].strip("-")
    return f"{slug or 'repo'}-{digest[:_REPO_KEY_DIGEST]}"


def default_logs_dir(repo: object = None) -> Path:
    """Where harness.core writes structured state (Task log root).

    ``HARNESS_LOGS_DIR`` wins when set; otherwise the harness-owned home
    keeps one directory per repository, OUTSIDE the user's working tree.
    """
    env = _env_path("HARNESS_LOGS_DIR")
    if env is not None:
        return env
    return neo_home() / "logs" / repo_key(repo)


# ---------------------------------------------------------------------------
# Forced in-repo log roots: gitignore + warn
# ---------------------------------------------------------------------------


def find_git_root(start: object = None) -> Optional[Path]:
    """Nearest ancestor directory containing ``.git``; None when absent.

    Walks up from `start` (default: the CWD) and stops at the filesystem
    root. Never raises; a permission error simply ends the walk.
    """
    current = canonical_repo(start)
    for candidate in [current, *current.parents]:
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError:
            return None
    return None


def is_within(child: object, parent: object) -> bool:
    """True when `child` resolves to `parent` or a directory beneath it."""
    try:
        resolved_child = canonical_repo(child)
        resolved_parent = canonical_repo(parent)
    except (OSError, ValueError):
        return False
    if resolved_child == resolved_parent:
        return True
    try:
        resolved_child.relative_to(resolved_parent)
    except ValueError:
        return False
    return True


def _gitignore_covers(text: str, entry: str) -> bool:
    """True when `.gitignore` text already ignores `entry`.

    Deliberately duplicated from ``cli.neoconfig._gitignore_covers`` rather
    than imported: the dependency direction is cli -> memory, so memory may
    not import cli. Exact-line, whole-dir, and basename-glob coverage are
    all accepted; the comparison is case-insensitive because git's
    ignore matching is case-sensitive but Windows paths are not.
    """
    base = entry.rstrip("/").rsplit("/", 1)[-1]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        negative = False
        while line.startswith("!"):
            negative = not negative
            line = line[1:].strip()
        if not line:
            continue
        candidate = line.rstrip("/").lstrip("/")
        if negative:
            continue
        if candidate in ("*", "**", "/*"):
            return True
        if candidate.casefold() == entry.casefold():
            return True
        if candidate.casefold() == base.casefold():
            return True
        if any(ch in candidate for ch in "*?["):
            if fnmatch.fnmatch(entry.casefold(), candidate.casefold()):
                return True
            if fnmatch.fnmatch(base.casefold(), candidate.casefold()):
                return True
    return False


def log_root_placement(log_root: object, repo: object = None) -> dict:
    """Describe where `log_root` sits relative to the user's repository.

    Returns a receipt dict (never raises):

    ``log_root``   the resolved path
    ``repo_root``  the enclosing git repository, or None
    ``in_repo``    True when the log root is inside that repository
    ``entry``      the repository-relative ``.gitignore`` entry that would
                   cover it (empty when outside the repo)
    ``covered``    True when an existing .gitignore already covers it
    """
    root = canonical_repo(log_root)
    repository = find_git_root(repo if repo is not None else root)
    result = {
        "log_root": str(root),
        "repo_root": str(repository) if repository is not None else "",
        "in_repo": False,
        "entry": "",
        "covered": False,
    }
    if repository is None or not is_within(root, repository):
        return result
    try:
        relative = root.relative_to(repository).as_posix()
    except ValueError:
        return result
    if not relative or relative == ".":
        return result
    result["in_repo"] = True
    result["entry"] = relative
    try:
        text = (repository / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    result["covered"] = _gitignore_covers(text, relative)
    return result


def ensure_log_root_ignored(log_root: object, repo: object = None) -> dict:
    """Add a FORCED in-repo log root to ``.gitignore`` and describe the result.

    A run directory holds a full ``pristine``/``work`` copy of the target
    source tree; leaving that inside an unignored repository directory is
    how a user's repo silently gains a second copy of itself. This adds the
    repository-relative entry to ``.gitignore`` (idempotently, creating the
    file only inside a real repository) and returns a receipt carrying a
    human-readable ``warning`` whenever artifacts still land in the repo.

    Best effort by contract: a read-only checkout, a non-repository path, or
    any IO failure returns the receipt with ``added=False`` and a warning
    rather than raising. Callers print the warning; nothing is silent.
    """
    receipt = log_root_placement(log_root, repo)
    receipt["added"] = False
    receipt["warning"] = ""
    if not receipt["in_repo"]:
        return receipt
    entry = str(receipt["entry"])
    repository = Path(str(receipt["repo_root"]))
    if receipt["covered"]:
        receipt["warning"] = (
            f"artifact root {entry}/ is inside {receipt['repo_root']} and is "
            "gitignored — a verified run stores a copy of the source tree there"
        )
        return receipt
    try:
        gitignore = repository / ".gitignore"
        existing = ""
        if gitignore.is_file():
            existing = gitignore.read_text(encoding="utf-8", errors="replace")
            if _gitignore_covers(existing, entry):
                receipt["covered"] = True
                receipt["warning"] = (
                    f"artifact root {entry}/ is inside {receipt['repo_root']} "
                    "and is gitignored"
                )
                return receipt
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}{entry}/\n")
        receipt["added"] = True
        receipt["covered"] = True
        receipt["warning"] = (
            f"artifact root {entry}/ is inside {receipt['repo_root']} — added it "
            "to .gitignore; prefer a repo-outside root "
            "(`neo config unset log_root`) to keep the repo clean"
        )
    except OSError as exc:
        receipt["warning"] = (
            f"artifact root {entry}/ is inside {receipt['repo_root']} and is NOT "
            f"gitignored ({type(exc).__name__}); a run stores a copy of the source "
            "tree there — add it to .gitignore or move the log root outside the repo"
        )
    return receipt


def artifact_root_warnings(receipts: List[dict]) -> Tuple[str, ...]:
    """Unique, non-empty warnings from one or more placement receipts."""
    seen: List[str] = []
    for receipt in receipts or []:
        warning = str((receipt or {}).get("warning") or "")
        if warning and warning not in seen:
            seen.append(warning)
    return tuple(seen)


# ---------------------------------------------------------------------------
# Task-id containment guard (Round 6 adversarial hardening)
# ---------------------------------------------------------------------------

_BAD_TASK_ID_CHARS = set('/\\:*?"<>|\x00')


def is_safe_task_id(task_id: str) -> bool:
    """True iff `task_id` is safe to join onto a logs root as ONE segment.

    Blocks every traversal/escape form (all verified live on Windows):
    - separators ``/`` ``\\`` and null bytes
    - drive/UNC forms via ``:`` (``C:`` is DRIVE-RELATIVE on Win32 —
      ``Path(base) / 'C:evil'`` discards the base entirely)
    - edge whitespace/dot tricks: Win32 path normalization strips
      leading/trailing spaces and trailing dots, so ``' ..'`` resolves
      AS ``..`` and ``'x.'`` aliases ``x`` — both directions rejected
    - ``.`` / ``..`` / all-dot segments
    Interior spaces/unicode are allowed (legitimate user-chosen ids);
    anything rejected simply cannot name a real task directory.
    """
    if not isinstance(task_id, str) or not task_id:
        return False
    if any(c in _BAD_TASK_ID_CHARS for c in task_id):
        return False
    if task_id != task_id.strip():
        return False  # edge whitespace smuggles '..' past Win32 normalize
    # Win32-equivalent segment: strip trailing dots/spaces, then check
    normalized = task_id.rstrip(". ")
    return bool(normalized and normalized not in (".", ".."))


def safe_task_dir(task_id: str, logs_root: Optional[Path] = None) -> Optional[Path]:
    """logs_root/<task_id>/ when `task_id` is a single safe segment, else
    None. Belt-and-suspenders: even a pattern-allowed id must RESOLVE
    inside the logs root (raises nothing; returns None on any doubt)."""
    if not is_safe_task_id(task_id):
        return None
    root = Path(logs_root) if logs_root is not None else default_logs_dir()
    d = root / task_id
    try:
        d.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return None
    return d


def safe_state_file(state_file: Path, logs_root: Path) -> Optional[Path]:
    """Return a resolved state file only when it is a regular file contained
    by logs_root. Direct state-file symlinks and resolved outside-root targets
    are rejected; path errors return None rather than escaping the logs scope.
    """
    try:
        root = Path(logs_root).expanduser().resolve()
        candidate = Path(state_file).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve()
        resolved.relative_to(root)
        if not resolved.is_file():
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Snapshot artifacts: the shared content store, the retention policy, and the
# one read-only receipt `cli.doctor` renders as its disk section (R2-10).
# ---------------------------------------------------------------------------

#: Name of the content store inside a logs root. It is deliberately NOT a run
#: directory, so `execution.snapshot.is_run_directory` skips it and retention
#: pruning can never sweep the shared store away as if it were one task.
SNAPSHOT_STORE_DIRNAME = "_snapshot-store"


def snapshot_store_root(log_root: object = None, repo: object = None) -> Path:
    """Return the root of the shared snapshot content store.

    ``NEO_SNAPSHOT_STORE`` wins outright. Otherwise the store lives at
    ``<log_root>/_snapshot-store`` -- inside the logs root rather than beside
    it, so one repo's content is never shared across repositories and the
    retention pass that owns the log root also owns the store. Never raises:
    an unresolvable path is still returned so a caller can attempt the write
    and report the real failure at first use.
    """
    override = _env_path("NEO_SNAPSHOT_STORE", "VEX_SNAPSHOT_STORE")
    if override is not None:
        return override
    root = Path(log_root) if log_root not in (None, "") else default_logs_dir(repo)
    return Path(root) / SNAPSHOT_STORE_DIRNAME


def retention_receipt(
    log_root: object = None,
    repo: object = None,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the retention policy that would apply to ``log_root`` today.

    Read-only and total: an unimportable ``execution.snapshot``, an unreadable
    root, or an absent directory all produce a receipt with an ``error`` and
    the fields that could be resolved, never an exception. This is the one
    place the policy is described, so ``neo doctor`` and an operator reading a
    run's trace cannot be told two different retention stories.
    """
    root = Path(log_root) if log_root not in (None, "") else default_logs_dir(repo)
    store = snapshot_store_root(root, repo=repo)
    receipt: Dict[str, Any] = {
        "log_root": str(root),
        "log_root_exists": root.is_dir(),
        "store_root": str(store),
        "policy": None,
        "error": "",
    }
    try:
        from execution import snapshot as snapshot_engine
    except Exception as exc:  # a missing engine must not break /doctor
        receipt["error"] = f"execution.snapshot is unavailable: {type(exc).__name__}"
        return receipt
    try:
        receipt["policy"] = snapshot_engine.resolve_retention(
            config if isinstance(config, Mapping) else None
        ).to_dict()
    except Exception as exc:
        receipt["error"] = f"retention policy is unresolvable: {type(exc).__name__}"
    return receipt


def snapshot_disk_receipt(
    log_root: object = None,
    repo: object = None,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return measured disk facts for the run root and the shared store.

    Everything ``cli doctor``'s disk section reports comes from here, so the
    human and ``--json`` renderings cannot disagree and an operator is looking
    at the same numbers a prune would act on. Total by contract: it never
    raises, and a field it could not measure is reported as ``None`` with the
    reason in ``error`` rather than as a zero that reads like "nothing there".
    """
    root = Path(log_root) if log_root not in (None, "") else default_logs_dir(repo)
    store_root = snapshot_store_root(root, repo=repo)
    receipt: Dict[str, Any] = {
        "log_root": str(root),
        "log_root_exists": False,
        "total_bytes": None,
        "run_directories": None,
        "largest": [],
        "pristine_bytes": None,
        "work_bytes": None,
        "store_root": str(store_root),
        "store": None,
        "free_bytes": None,
        "total_capacity_bytes": None,
        "budget": None,
        "retention": retention_receipt(root, repo=repo, config=config)["policy"],
        "error": "",
    }
    try:
        from execution import snapshot as snapshot_engine
    except Exception as exc:
        receipt["error"] = f"execution.snapshot is unavailable: {type(exc).__name__}"
        return receipt

    store = snapshot_engine.SharedStore(store_root)
    try:
        receipt["store"] = store.stats()
    except Exception as exc:
        receipt["error"] = f"store stats failed: {type(exc).__name__}"
    try:
        probe = root if root.exists() else Path(root.anchor or root)
        usage = shutil.disk_usage(str(probe))
        receipt["free_bytes"] = int(usage.free)
        receipt["total_capacity_bytes"] = int(usage.total)
    except Exception as exc:
        receipt["error"] = f"free space is unmeasurable: {type(exc).__name__}"
    if not root.is_dir():
        return receipt
    receipt["log_root_exists"] = True

    total = 0
    pristine = 0
    work = 0
    runs = 0
    largest: List[Tuple[str, int]] = []
    try:
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if not snapshot_engine.is_run_directory(entry):
                continue
            runs += 1
            size = snapshot_engine.tree_bytes(entry)
            total += size
            pristine += snapshot_engine.tree_bytes(entry / "pristine")
            work += snapshot_engine.tree_bytes(entry / "work")
            largest.append((entry.name, size))
    except OSError as exc:
        receipt["error"] = f"run root walk failed: {type(exc).__name__}"
    largest.sort(key=lambda item: (-item[1], item[0]))
    receipt["total_bytes"] = total
    receipt["run_directories"] = runs
    receipt["pristine_bytes"] = pristine
    receipt["work_bytes"] = work
    receipt["largest"] = [{"name": name, "bytes": size} for name, size in largest[:5]]
    try:
        settings = snapshot_engine.resolve_budget(
            config if isinstance(config, Mapping) else None
        )
        probe = snapshot_engine.SnapshotPlan(
            source="",
            destination=str(root),
            total_bytes=total,
        )
        receipt["budget"] = snapshot_engine.enforce_budget(
            probe, settings, free_bytes=receipt["free_bytes"]
        ).to_dict()
    except Exception as exc:
        receipt["error"] = f"budget could not be evaluated: {type(exc).__name__}"
    return receipt


def prune_snapshot_artifacts(
    log_root: object = None,
    repo: object = None,
    *,
    dry_run: bool = False,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply (or, with ``dry_run``, report) the retention policy for a root.

    Delegates to :func:`execution.snapshot.prune_run_directories` and returns
    its JSON-compatible report, so the operator surface
    (``python -m execution.snapshot --prune``, and whatever the CLI wires to
    it) and a programmatic caller remove exactly the same set. Total by
    contract: a failure is reported as ``{"ok": False, "error": ...}`` rather
    than raised, because a prune that dies half way through must not look like
    a completed one.
    """
    root = Path(log_root) if log_root not in (None, "") else default_logs_dir(repo)
    receipt: Dict[str, Any] = {
        "ok": False,
        "dry_run": bool(dry_run),
        "log_root": str(root),
        "removed": [],
        "bytes_removed": 0,
        "skipped": [],
        "blobs_removed": 0,
        "errors": [],
        "error": "",
    }
    try:
        from execution import snapshot as snapshot_engine
    except Exception as exc:
        receipt["error"] = f"execution.snapshot is unavailable: {type(exc).__name__}"
        return receipt
    try:
        store = snapshot_engine.SharedStore(snapshot_store_root(root, repo=repo))
        report = snapshot_engine.prune_run_directories(
            root,
            snapshot_engine.resolve_retention(
                config if isinstance(config, Mapping) else None
            ),
            dry_run=bool(dry_run),
            store=store,
        )
    except Exception as exc:
        receipt["error"] = f"prune failed: {type(exc).__name__}: {exc}"
        return receipt
    payload = report.to_dict()
    receipt.update(
        {
            "ok": not payload["errors"],
            "removed": payload["removed"],
            "bytes_removed": payload["bytes_removed"],
            "skipped": payload["skipped"],
            "blobs_removed": payload["blobs_removed"],
            "errors": payload["errors"],
            "kept": payload["kept"],
        }
    )
    return receipt
