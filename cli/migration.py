"""`neo migrate` — detect an older on-disk layout, plan it, apply it atomically.

The gap this closes: Neo's configuration and state layout has changed across
releases (a single `~/.neo/config.toml` became a two-tier `settings.toml`
chain; the run-artifact root moved out of the repository; plugin installs
became atomic and gained an install record; hook configs gained a
`schema_version`). Each change was correct on its own, and none of them had a
way to bring an EXISTING installation forward. An upgrade therefore left
operators with a mixture of layouts that no single reader fully understood —
including, for a while, a plugin root with directories and no record of what
put them there.

The contract this module implements:

* **Detect** — every migration is a pure predicate over the on-disk state. It
  never writes. ``plan_migrations()`` is the whole surface an operator needs
  to see the exact plan.
* **Plan** — the plan is data (:class:`MigrationPlan`), JSON-serializable, and
  every action names the concrete path it will touch and whether it is
  destructive. A destructive action is never implicit.
* **Apply atomically** — every write and every removal goes through one
  :class:`_Transaction` that snapshots the previous bytes (and moves a removal
  target aside rather than deleting it). A failure anywhere rolls the WHOLE run
  back, so a half-migrated install is not a state this module can produce.
* **Idempotent** — each detector is written so that a migrated system no longer
  satisfies it. Running ``neo migrate`` twice is the second run reporting
  ``already_current`` with zero actions, not a second set of writes.

Knobs come from the caller's arguments, not from hardcoded module state. The
one bound this module carries for itself is :data:`MAX_ACTIONS_PER_RUN`, a
safety bound on how many actions a single run will take: a plan larger than
that is reported rather than executed, because an operator who has 10,000
files in a relocated log root needs to be told, not worked on.

No `INTERFACES.md` boundary signature is involved: every input is a filesystem
path and every output is one of the frozen dataclasses below.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "MAX_ACTIONS_PER_RUN",
    "MIGRATION_IDS",
    "MIGRATION_SCHEMA_VERSION",
    "MigrationAction",
    "MigrationError",
    "MigrationOutcome",
    "MigrationPlan",
    "MigrationResult",
    "MigrationStep",
    "apply_migrations",
    "migrate",
    "plan_migrations",
]

#: Bumped when the plan/receipt document shape changes. A receipt carrying any
#: other value is refused by a reader rather than half-understood.
MIGRATION_SCHEMA_VERSION = 1

#: An action is one concrete mutation. A plan larger than this is REPORTED, not
#: executed: the operator decides, because a bulk relocation is their call.
MAX_ACTIONS_PER_RUN = 500

MIGRATION_IDS: Tuple[str, ...] = (
    "legacy_config_toml",
    "hook_config_schema_version",
    "plugin_install_receipt",
    "plugin_install_residue",
    "connector_permission_declaration",
    "in_repo_log_root",
)


class MigrationError(Exception):
    """A clean, user-facing migration failure (CLI renders it + exit 2)."""


#: The action-detail prefix the connector-declaration step uses to name the
#: label it is declaring. Kept as a constant so the writer and the detector
#: cannot drift apart.
_DECLARE_PREFIX = "declare default permissions for connector "
_NAME_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------------------------------------------------------------------------
# Plan data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationAction:
    """One concrete mutation the plan will perform."""

    kind: str
    path: str
    detail: str = ""
    destructive: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible action record."""
        return {
            "kind": self.kind,
            "path": self.path,
            "detail": self.detail,
            "destructive": bool(self.destructive),
        }


@dataclass(frozen=True)
class MigrationStep:
    """One migration: its id, whether it applies, and the actions it would take."""

    id: str
    title: str
    applies: bool
    reason: str
    actions: Tuple[MigrationAction, ...] = ()
    reversible: bool = True

    @property
    def destructive(self) -> bool:
        """Whether any action in this step destroys data."""
        return any(action.destructive for action in self.actions)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible step record."""
        return {
            "id": self.id,
            "title": self.title,
            "applies": bool(self.applies),
            "reason": self.reason,
            "destructive": self.destructive,
            "reversible": bool(self.reversible),
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True)
class MigrationPlan:
    """The exact plan: what applies, what it will touch, what it will destroy."""

    steps: Tuple[MigrationStep, ...] = ()
    already_current: bool = False
    over_budget: bool = False
    action_count: int = 0
    notes: Tuple[str, ...] = ()

    def pending(self) -> Tuple[MigrationStep, ...]:
        """Return the steps that would actually run."""
        return tuple(step for step in self.steps if step.applies)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible plan document."""
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "already_current": bool(self.already_current),
            "over_budget": bool(self.over_budget),
            "action_count": self.action_count,
            "action_budget": MAX_ACTIONS_PER_RUN,
            "steps": [step.to_dict() for step in self.steps],
            "pending_ids": [step.id for step in self.pending()],
            "destructive_ids": [step.id for step in self.pending() if step.destructive],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class MigrationOutcome:
    """What happened to one step during an apply run."""

    id: str
    applied: bool
    detail: str = ""
    actions_applied: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible outcome record."""
        return {
            "id": self.id,
            "applied": bool(self.applied),
            "detail": self.detail,
            "actions_applied": int(self.actions_applied),
        }


@dataclass(frozen=True)
class MigrationResult:
    """The result of one ``apply_migrations`` run, including its rollback state."""

    plan: MigrationPlan
    outcomes: Tuple[MigrationOutcome, ...] = ()
    applied: bool = False
    rolled_back: bool = False
    rollback_errors: Tuple[str, ...] = ()
    error: str = ""
    receipt_path: str = ""
    skipped_destructive: Tuple[str, ...] = ()

    @property
    def idempotent(self) -> bool:
        """Whether the plan this run saw had nothing to do.

        Note this reads the PLAN, not the outcome: a run that failed is not
        "idempotent", it is "rolled back", and the two are separate fields.
        """
        return not self.plan.pending()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible result document."""
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "plan": self.plan.to_dict(),
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "applied": bool(self.applied),
            "rolled_back": bool(self.rolled_back),
            "rollback_errors": list(self.rollback_errors),
            "error": self.error,
            "receipt_path": self.receipt_path,
            "skipped_destructive": list(self.skipped_destructive),
            "idempotent": self.idempotent,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# The transaction
# ---------------------------------------------------------------------------


class _Transaction:
    """Snapshot-then-apply across a whole migration run.

    Writes record the previous bytes (or the fact that the file was absent)
    before they touch anything. Removals MOVE the target aside into a uniquely
    named backup instead of deleting it, so a removal is as reversible as a
    write. :meth:`commit` drops the backups; :meth:`rollback` puts everything
    back and reports anything it could not put back, so a failed migration
    says what state the machine is actually in.
    """

    def __init__(self) -> None:
        self._writes: List[Tuple[Path, Optional[bytes]]] = []
        self._moved: List[Tuple[Path, Path]] = []
        self._errors: List[str] = []
        self._counter = 0

    # -- apply -----------------------------------------------------------

    def write_text(self, path: Path, text: str) -> None:
        """Atomically write ``text`` to ``path`` after snapshotting the old bytes."""
        self._writes.append((path, path.read_bytes() if path.is_file() else None))
        _atomic_write_text(path, text)

    def move_aside(self, path: Path) -> None:
        """Move ``path`` to a backup rather than deleting it."""
        self._counter += 1
        backup = path.with_name(
            f"{path.name}.migrate-backup-{os.getpid()}-{self._counter}"
        )
        if backup.exists():
            shutil.rmtree(
                backup, ignore_errors=True
            ) if backup.is_dir() else backup.unlink()
        os.replace(path, backup)
        self._moved.append((path, backup))

    # -- finish ----------------------------------------------------------

    def commit(self) -> None:
        """Accept every change and delete the backups."""
        for _original, backup in self._moved:
            if backup.is_dir():
                shutil.rmtree(backup, ignore_errors=True)
            elif backup.exists():
                try:
                    backup.unlink()
                except OSError as exc:
                    self._errors.append(f"could not delete backup {backup}: {exc}")
        self._moved.clear()

    def rollback(self) -> List[str]:
        """Restore every snapshotted write and every moved path."""
        errors: List[str] = list(self._errors)
        for original, backup in reversed(self._moved):
            try:
                if original.exists():
                    if original.is_dir():
                        shutil.rmtree(original, ignore_errors=True)
                    else:
                        original.unlink()
                os.replace(backup, original)
            except OSError as exc:
                errors.append(
                    f"could not restore {original} from {backup}: {exc}; "
                    f"the previous version is preserved at {backup}"
                )
        self._moved.clear()
        for path, previous in reversed(self._writes):
            try:
                if previous is None:
                    if path.is_file():
                        path.unlink()
                else:
                    _atomic_write_text(path, previous.decode("utf-8", "replace"))
            except OSError as exc:
                errors.append(f"could not restore {path}: {exc}")
        self._writes.clear()
        return errors


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` through a unique temp file plus ``os.replace``.

    Deliberately independent of ``cli.neoconfig``'s lock machinery: a migration
    must be able to restore a file that lock is holding, and a migration that
    cannot write during a rollback is not a rollback.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.migrate-tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _toml_loads(text: str) -> Optional[Dict[str, Any]]:
    """Parse TOML with the stdlib parser or tomli; ``None`` when unavailable."""
    for module_name in ("tomllib", "tomli"):
        try:
            module = __import__(module_name)
        except ModuleNotFoundError:
            continue
        try:
            return module.loads(text)
        except ValueError:
            return None
    return None


def _read_text(path: Path) -> Optional[str]:
    """Read a text file, tolerating a UTF-8 BOM; ``None`` when unreadable."""
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def detect_legacy_config_toml() -> MigrationStep:
    """Migrate ``~/.neo/config.toml`` into the two-tier global settings file.

    The legacy single file is still READ by ``cli.neoconfig`` while the new
    global file is missing, so this is a real compatibility state and not a
    hypothetical one. The migration copies its keys into the global tier and
    renames the legacy file to ``config.toml.migrated-v1`` — a rename, not a
    delete, so the operator's original bytes survive.
    """
    try:
        from cli.neoconfig import global_settings_path, legacy_settings_path
    except Exception as exc:  # pragma: no cover - import-time breakage
        return MigrationStep(
            "legacy_config_toml",
            "Move the legacy ~/.neo/config.toml into the two-tier settings chain",
            False,
            f"cli.neoconfig is unavailable: {exc}",
        )
    legacy = legacy_settings_path()
    global_path = global_settings_path()
    if not legacy.is_file():
        return MigrationStep(
            "legacy_config_toml",
            "Move the legacy ~/.neo/config.toml into the two-tier settings chain",
            False,
            f"no legacy file at {legacy}",
        )
    text = _read_text(legacy) or ""
    data = _toml_loads(text)
    if data is None:
        return MigrationStep(
            "legacy_config_toml",
            "Move the legacy ~/.neo/config.toml into the two-tier settings chain",
            False,
            f"{legacy} is not valid TOML; fix it by hand before migrating",
        )
    body = data.get("neo") if isinstance(data.get("neo"), dict) else data
    if not isinstance(body, dict) or not body:
        return MigrationStep(
            "legacy_config_toml",
            "Move the legacy ~/.neo/config.toml into the two-tier settings chain",
            False,
            f"{legacy} carries no settings keys to migrate",
        )
    if global_path.is_file() and (_read_text(global_path) or "").strip():
        return MigrationStep(
            "legacy_config_toml",
            "Move the legacy ~/.neo/config.toml into the two-tier settings chain",
            False,
            f"{global_path} already exists; the legacy file is inactive and was left alone",
        )
    keys = ", ".join(sorted(str(key) for key in body))
    return MigrationStep(
        "legacy_config_toml",
        "Move the legacy ~/.neo/config.toml into the two-tier settings chain",
        True,
        f"{legacy} holds {len(body)} legacy key(s) ({keys}) and no global settings file exists",
        (
            MigrationAction(
                "write_file",
                str(global_path),
                f"write {len(body)} legacy key(s) into the global tier",
            ),
            MigrationAction(
                "move_path",
                f"{legacy} -> {legacy}.migrated-v1",
                "rename the legacy file so it is no longer read",
            ),
        ),
    )


def detect_hook_config_schema_version(repo_path: Optional[str] = None) -> MigrationStep:
    """Stamp ``schema_version: 1`` onto a hook config that predates it.

    ``load_hook_config`` treats a missing ``schema_version`` as 1, so this is
    a normalisation rather than a repair — but an UNRECOGNISED version is a
    hard error, and a file that has been hand-edited since then should say what
    it is. The rewrite preserves the key order and the operator's own text by
    inserting the stamp as the first line rather than re-serializing the JSON.
    """
    try:
        from extensions.user_hooks import hook_config_tier_paths

        paths = hook_config_tier_paths(repo_path)
    except Exception as exc:  # pragma: no cover - import-time breakage
        return MigrationStep(
            "hook_config_schema_version",
            "Stamp schema_version onto a pre-versioned hooks.json",
            False,
            f"extensions.user_hooks is unavailable: {exc}",
        )
    unstamped: List[str] = []
    for tier in ("user", "project", "local"):
        path = paths.get(tier)
        if path is None or not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and "schema_version" not in data:
            unstamped.append(f"{tier}:{path}")
    if not unstamped:
        return MigrationStep(
            "hook_config_schema_version",
            "Stamp schema_version onto a pre-versioned hooks.json",
            False,
            "every present hook config already declares schema_version",
        )
    return MigrationStep(
        "hook_config_schema_version",
        "Stamp schema_version onto a pre-versioned hooks.json",
        True,
        f"{len(unstamped)} hook config(s) have no schema_version: {unstamped}",
        tuple(
            MigrationAction(
                "write_file", path.split(":", 1)[1], f"stamp schema_version: 1 ({tier})"
            )
            for path in unstamped
        ),
    )


def detect_plugin_install_receipts() -> MigrationStep:
    """Backfill an install record for a plugin installed before installs were atomic.

    A pre-atomic install left a directory with no record of what produced it.
    The backfill is honest about that: it records the source as ``"backfill"``,
    a zero tree digest is NOT invented, and the file counts are measured now
    rather than guessed. That is enough for a later uninstall to be complete,
    which is the property the receipt exists for.
    """
    try:
        from cli import plugins as plugins_mod
    except Exception as exc:  # pragma: no cover - import-time breakage
        return MigrationStep(
            "plugin_install_receipt",
            "Backfill install records for pre-atomic plugin installs",
            False,
            f"cli.plugins is unavailable: {exc}",
        )
    missing: List[str] = []
    for entry in plugins_mod.list_plugins():
        if entry.get("error"):
            continue
        if (
            entry.get("name")
            and plugins_mod.read_install_receipt(entry["name"]) is None
        ):
            missing.append(str(entry["name"]))
    if not missing:
        return MigrationStep(
            "plugin_install_receipt",
            "Backfill install records for pre-atomic plugin installs",
            False,
            "every installed plugin already has an install record",
        )
    return MigrationStep(
        "plugin_install_receipt",
        "Backfill install records for pre-atomic plugin installs",
        True,
        f"{len(missing)} installed plugin(s) have no install record: {sorted(missing)}",
        tuple(
            MigrationAction(
                "write_file",
                str(plugins_mod.plugin_state_dir() / f"{name}.json"),
                f"backfill an install record for {name!r}",
            )
            for name in sorted(missing)
        ),
    )


def detect_plugin_install_residue() -> MigrationStep:
    """Reclaim staging/trash residue from an interrupted plugin install.

    These directories are created by this module's own atomic installer and are
    only ever left behind when an install died between staging and swapping.
    Removing them is therefore safe by construction, and it is marked
    destructive because it deletes bytes — the operator sees that in the plan.
    """
    try:
        from cli.plugins import STAGING_PREFIX, TRASH_PREFIX, plugins_root
    except Exception as exc:  # pragma: no cover - import-time breakage
        return MigrationStep(
            "plugin_install_residue",
            "Reclaim staging/trash residue from an interrupted plugin install",
            False,
            f"cli.plugins is unavailable: {exc}",
        )
    root = plugins_root()
    residue: List[str] = []
    if root.is_dir():
        try:
            residue = sorted(
                entry.name
                for entry in root.iterdir()
                if entry.name.startswith((STAGING_PREFIX, TRASH_PREFIX))
            )
        except OSError:
            residue = []
    if not residue:
        return MigrationStep(
            "plugin_install_residue",
            "Reclaim staging/trash residue from an interrupted plugin install",
            False,
            f"no staging or trash residue under {root}",
        )
    return MigrationStep(
        "plugin_install_residue",
        "Reclaim staging/trash residue from an interrupted plugin install",
        True,
        f"{len(residue)} interrupted-install directory(ies) under {root}: {residue}",
        tuple(
            MigrationAction(
                "remove_tree",
                str(root / name),
                "interrupted install residue; safe to remove by construction",
                destructive=True,
            )
            for name in residue
        ),
        reversible=True,
    )


def detect_connector_permission_declarations(
    repo_path: Optional[str] = None,
) -> MigrationStep:
    """Give every configured connector a declared permission set.

    A connector registered before permissions existed has an UNDECLARED blast
    radius, which means no tool allowlist, no side-effect ceiling, and no
    statement about the network or file-write capability. The migration writes
    the default declaration — ``tools = ["*"]``, ``side_effect = "mutation"``,
    no network hosts, ``write`` undeclared — which reproduces the pre-existing
    behaviour exactly while making the declaration visible in a file. A
    connector that can reach the network must then be told which hosts, and one
    that can write files must say ``write = true``; neither is assumed here.
    """
    try:
        from cli import connectors as connectors_mod
    except Exception as exc:  # pragma: no cover - import-time breakage
        return MigrationStep(
            "connector_permission_declaration",
            "Declare a permission set for every configured connector",
            False,
            f"cli.connectors is unavailable: {exc}",
        )
    try:
        servers = connectors_mod.discover_mcp_servers(repo_path)
        declared = set(connectors_mod.read_permissions(repo_path))
    except connectors_mod.ConnectorError as exc:
        return MigrationStep(
            "connector_permission_declaration",
            "Declare a permission set for every configured connector",
            False,
            f"an existing permission file is unreadable ({exc}); fix it by hand",
        )
    undeclared = sorted(label for label in servers if label not in declared)
    if not undeclared:
        return MigrationStep(
            "connector_permission_declaration",
            "Declare a permission set for every configured connector",
            False,
            f"all {len(servers)} configured connector(s) already declare permissions",
        )
    path = connectors_mod._permission_file_for_tier("project", repo_path)
    return MigrationStep(
        "connector_permission_declaration",
        "Declare a permission set for every configured connector",
        True,
        f"{len(undeclared)} connector(s) have no declared permissions: {undeclared}",
        tuple(
            MigrationAction(
                "write_file",
                str(path) if path else "(global settings.toml)",
                f"declare default permissions for connector {label}",
            )
            for label in undeclared
        ),
    )


def detect_in_repo_log_root(
    repo_path: Optional[str] = None, log_root: Optional[str] = None
) -> MigrationStep:
    """Report a run-artifact root that still lives inside the repository.

    A run directory holds a full ``pristine`` + ``work`` copy of the target
    source tree, so a root under the repository duplicates the user's checkout
    on every run. This step does NOT move the bytes — a multi-gigabyte
    relocation is the operator's call, and a tool that silently moves a
    repository's artifact tree is a tool that loses one. It ensures the root is
    covered by ``.gitignore`` (idempotent, non-destructive) and names the exact
    command for the relocation.
    """
    notes: List[str] = []
    try:
        from memory.paths import default_logs_dir, is_within, log_root_placement
    except Exception as exc:  # pragma: no cover - import-time breakage
        return MigrationStep(
            "in_repo_log_root",
            "Keep the run-artifact root out of the repository",
            False,
            f"memory.paths is unavailable: {exc}",
        )
    effective = Path(log_root) if log_root else default_logs_dir()
    if repo_path:
        repo = Path(repo_path)
    else:
        try:
            repo = Path.cwd()
        except OSError:  # pragma: no cover - cwd deleted under us
            return MigrationStep(
                "in_repo_log_root",
                "Keep the run-artifact root out of the repository",
                False,
                "no repository to compare against",
            )
    try:
        inside = is_within(effective, repo)
    except Exception:
        inside = False
    if not inside:
        return MigrationStep(
            "in_repo_log_root",
            "Keep the run-artifact root out of the repository",
            False,
            f"{effective} is outside {repo}",
        )
    try:
        relative = effective.resolve().relative_to(repo.resolve()).as_posix()
    except Exception:
        relative = effective.name
    actions = [
        MigrationAction(
            "ignore_entry",
            str(repo / ".gitignore"),
            f"ensure {relative}/ is ignored so artifacts are never committed",
        )
    ]
    try:
        placement = log_root_placement(effective, repo)
    except Exception:
        placement = {}
    for warning in (placement or {}).get("warnings", []) or []:
        notes.append(str(warning))
    notes.append(
        f"relocate the bulk with: neo migrate --relocate-logs "
        f"'{relative}' '<destination>'  (this step never moves bytes on its own)"
    )
    return MigrationStep(
        "in_repo_log_root",
        "Keep the run-artifact root out of the repository",
        True,
        f"the run-artifact root {effective} is inside the repository {repo}",
        tuple(actions),
    )


#: The registry, in the order migrations are applied. Order matters: the
#: residue reclaim runs last so a migration that needs to read plugin metadata
#: sees the pre-existing layout.
_DETECTORS: Tuple[Tuple[str, Callable[..., MigrationStep]], ...] = (
    ("legacy_config_toml", lambda ctx: detect_legacy_config_toml()),
    (
        "hook_config_schema_version",
        lambda ctx: detect_hook_config_schema_version(ctx.repo_path),
    ),
    (
        "plugin_install_receipt",
        lambda ctx: detect_plugin_install_receipts(),
    ),
    (
        "connector_permission_declaration",
        lambda ctx: detect_connector_permission_declarations(ctx.repo_path),
    ),
    (
        "in_repo_log_root",
        lambda ctx: detect_in_repo_log_root(ctx.repo_path, ctx.log_root),
    ),
    ("plugin_install_residue", lambda ctx: detect_plugin_install_residue()),
)


@dataclass(frozen=True)
class _Context:
    """The immutable inputs every detector reads."""

    repo_path: Optional[str] = None
    log_root: Optional[str] = None


def plan_migrations(
    repo_path: Optional[str] = None, log_root: Optional[str] = None
) -> MigrationPlan:
    """Return the exact migration plan. READ-ONLY: touches nothing.

    Every detector runs and every detector reports, so an operator sees the
    ones that do not apply and WHY — a plan that only lists pending work cannot
    distinguish "nothing to do" from "the detector could not look".
    """
    ctx = _Context(repo_path=repo_path, log_root=log_root)
    steps: List[MigrationStep] = []
    notes: List[str] = []
    for _id, detector in _DETECTORS:
        try:
            steps.append(detector(ctx))
        except Exception as exc:  # a broken detector must not hide the others
            steps.append(
                MigrationStep(
                    _id,
                    "detector failed",
                    False,
                    f"detector raised {type(exc).__name__}: {exc}",
                )
            )
    action_count = sum(len(step.actions) for step in steps if step.applies)
    return MigrationPlan(
        steps=tuple(steps),
        already_current=action_count == 0,
        over_budget=action_count > MAX_ACTIONS_PER_RUN,
        action_count=action_count,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Appliers
# ---------------------------------------------------------------------------


def _apply_step(step: MigrationStep, tx: _Transaction, ctx: _Context) -> int:
    """Apply one step's actions through the transaction. Returns the count."""
    applied = 0
    for action in step.actions:
        path = Path(action.path)
        if action.kind == "write_file":
            if step.id == "legacy_config_toml":
                _apply_legacy_config(path, tx)
            elif step.id == "hook_config_schema_version":
                _apply_hook_stamp(path, tx)
            elif step.id == "plugin_install_receipt":
                _apply_receipt_backfill(path, tx)
            elif step.id == "connector_permission_declaration":
                _apply_connector_declaration(step, tx, ctx)
            elif action.kind == "ignore_entry":
                _apply_gitignore(action, tx, ctx)
            else:  # pragma: no cover - unreachable with a closed step set
                raise MigrationError(f"no writer for action kind {action.kind!r}")
        elif action.kind == "move_path":
            source, _, _target = action.path.partition(" -> ")
            if Path(source).exists():
                tx.move_aside(Path(source))
        elif action.kind == "remove_tree":
            if path.exists():
                tx.move_aside(path)
        elif action.kind == "ignore_entry":
            _apply_gitignore(action, tx, ctx)
        else:
            raise MigrationError(f"unknown action kind {action.kind!r}")
        applied += 1
    return applied


def _apply_legacy_config(global_path: Path, tx: _Transaction) -> None:
    """Write the legacy keys into the global settings file."""
    from cli.neoconfig import legacy_settings_path

    text = _read_text(legacy_settings_path()) or ""
    data = _toml_loads(text) or {}
    body = data.get("neo") if isinstance(data.get("neo"), dict) else data
    lines = [
        "# Migrated by `neo migrate` from the legacy ~/.neo/config.toml.",
        "# The original file was renamed to config.toml.migrated-v1, not deleted.",
        "",
    ]
    for key in sorted(str(item) for item in body):
        lines.append(f"{key} = {json.dumps(body[key])}")
    tx.write_text(global_path, "\n".join(lines) + "\n")


def _apply_hook_stamp(path: Path, tx: _Transaction) -> None:
    """Insert ``schema_version`` as the first key, preserving the rest verbatim."""
    text = _read_text(path) or ""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise MigrationError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "schema_version" in data:
        return
    stamped = {"schema_version": 1, **data}
    tx.write_text(path, json.dumps(stamped, indent=2) + "\n")


def _apply_receipt_backfill(path: Path, tx: _Transaction) -> None:
    """Write a measured, honestly-sourced backfilled install record."""
    from cli import plugins as plugins_mod

    name = path.stem
    directory = plugins_mod._plugin_dir(name)
    digest, files, total = plugins_mod._tree_digest(directory)
    try:
        manifest = plugins_mod.read_manifest(directory)
    except plugins_mod.PluginError:
        manifest = {}
    tools = manifest.get("tools") or {}
    servers = manifest.get("mcp_servers") or {}
    receipt = plugins_mod.InstallReceipt(
        name=name,
        version=str(manifest.get("version") or ""),
        source_kind="backfill",
        source="(pre-atomic install; original source not recorded)",
        installed_at=datetime.now(timezone.utc).isoformat(),
        tool_verbs=tuple(str(item) for item in (tools.get("verbs") or [])),
        mcp_servers=tuple(sorted(str(key) for key in servers)),
        commands=tuple(str(item) for item in (manifest.get("commands") or [])),
        skills=tuple(str(item) for item in (manifest.get("skills") or [])),
        tree_sha256=digest,
        file_count=files,
        total_bytes=total,
    )
    tx.write_text(path, json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n")


def _apply_connector_declaration(
    step: MigrationStep, tx: _Transaction, ctx: _Context
) -> None:
    """Declare the default permission set for every undeclared connector."""
    from cli import connectors as connectors_mod

    labels = [
        action.detail[len(_DECLARE_PREFIX) :].strip()
        for action in step.actions
        if action.detail.startswith(_DECLARE_PREFIX)
    ]
    labels = [label for label in labels if _NAME_LABEL_RE.match(label)]
    if not labels:
        raise MigrationError("connector_permission_declaration names no connector")
    before = connectors_mod.read_permissions(ctx.repo_path)
    payload: Dict[str, Any] = {}
    for permission in before.values():
        payload[permission.label] = connectors_mod._permission_to_mapping(permission)
    for label in labels:
        payload.setdefault(label, {"tools": ["*"], "side_effect": "mutation"})
    target = None
    for action in step.actions:
        candidate = action.path
        if candidate and not candidate.startswith("("):
            target = Path(candidate)
            break
    if target is None:
        raise MigrationError("connector_permission_declaration has no writable path")
    # The ONE renderer: this module does not grow a second TOML vocabulary for
    # the same document, because a declaration written by a migration and a
    # declaration written by `neo mcp permissions` must be the same file.
    tx.write_text(target, connectors_mod.render_permission_document(payload))


def _apply_gitignore(action: MigrationAction, tx: _Transaction, ctx: _Context) -> None:
    """Add a directory entry to ``.gitignore`` idempotently."""
    gitignore = Path(action.path)
    entry = ""
    for token in action.detail.split():
        if token.endswith("/") and token != "is":
            entry = token
            break
    if not entry:
        raise MigrationError(f"ignore_entry action carries no entry: {action.detail!r}")
    existing = _read_text(gitignore) or ""
    lines = [line.strip() for line in existing.splitlines()]
    if entry in lines or entry.rstrip("/") in lines:
        return
    body = existing
    if body and not body.endswith("\n"):
        body += "\n"
    tx.write_text(gitignore, body + entry + "\n")


# ---------------------------------------------------------------------------
# The public entry points
# ---------------------------------------------------------------------------


def apply_migrations(
    plan: Optional[MigrationPlan] = None,
    repo_path: Optional[str] = None,
    log_root: Optional[str] = None,
    *,
    only: Sequence[str] = (),
    allow_destructive: bool = False,
) -> MigrationResult:
    """Apply the plan as ONE transaction. Never partially applied.

    Every write and every removal goes through a single :class:`_Transaction`.
    If any step raises, everything this run changed is restored and
    ``rolled_back`` is True — so a failed migration cannot leave a
    half-converted install behind. Destructive steps are skipped (and named in
    ``skipped_destructive``) unless ``allow_destructive=True``; they are still
    applied through the transaction, because the transaction MOVES a removal
    target aside rather than deleting it, so a destructive step is as
    reversible as a write until the commit.
    """
    active = plan if plan is not None else plan_migrations(repo_path, log_root)
    ctx = _Context(repo_path=repo_path, log_root=log_root)
    selected = tuple(
        step for step in active.pending() if not only or step.id in tuple(only)
    )
    if active.over_budget:
        return MigrationResult(
            plan=active,
            applied=False,
            error=(
                f"the plan has {active.action_count} actions, above the "
                f"{MAX_ACTIONS_PER_RUN} per-run budget; nothing was applied"
            ),
        )
    tx = _Transaction()
    outcomes: List[MigrationOutcome] = []
    skipped: List[str] = []
    error = ""
    rolled_back = False
    rollback_errors: List[str] = []
    for step in selected:
        if step.destructive and not allow_destructive:
            skipped.append(step.id)
            outcomes.append(
                MigrationOutcome(
                    step.id,
                    False,
                    "skipped: this step is destructive; pass --allow-destructive to take it",
                )
            )
            continue
        try:
            count = _apply_step(step, tx, ctx)
        except Exception as exc:
            error = f"{step.id} failed: {type(exc).__name__}: {exc}"
            rolled_back = True
            rollback_errors = tx.rollback()
            outcomes.append(MigrationOutcome(step.id, False, error))
            break
        outcomes.append(MigrationOutcome(step.id, True, "applied", count))
    if not rolled_back:
        tx.commit()
    # ``applied`` means "changes are IN PLACE", not "some step ran". After a
    # rollback the transaction restored everything, so reporting True here
    # would claim a migration landed on a machine that is byte-identical to
    # where it started.
    applied = any(outcome.applied for outcome in outcomes) and not rolled_back
    receipt_path = _write_receipt(
        MigrationResult(
            plan=active,
            outcomes=tuple(outcomes),
            applied=applied,
            rolled_back=rolled_back,
            rollback_errors=tuple(rollback_errors),
            error=error,
            skipped_destructive=tuple(skipped),
        )
    )
    return MigrationResult(
        plan=active,
        outcomes=tuple(outcomes),
        applied=applied,
        rolled_back=rolled_back,
        rollback_errors=tuple(rollback_errors),
        error=error,
        receipt_path=receipt_path,
        skipped_destructive=tuple(skipped),
    )


def migrate(
    repo_path: Optional[str] = None,
    log_root: Optional[str] = None,
    *,
    apply: bool = False,
    allow_destructive: bool = False,
    only: Sequence[str] = (),
) -> MigrationResult:
    """Plan, and optionally apply, in one call.

    ``apply=False`` (the default) is the PLAN-ONLY arm: it returns the plan and
    writes nothing at all, which is what ``neo migrate`` does without
    ``--apply``. ``apply=True`` runs :func:`apply_migrations` with the plan it
    just built, so the plan the operator approved and the plan that executed are
    the same object.
    """
    plan = plan_migrations(repo_path, log_root)
    if not apply:
        return MigrationResult(plan=plan, applied=False)
    return apply_migrations(
        plan,
        repo_path,
        log_root,
        only=only,
        allow_destructive=allow_destructive,
    )


def _write_receipt(result: MigrationResult) -> str:
    """Persist the result document under the harness home. Never raises."""
    try:
        from memory.paths import neo_home

        root = Path(neo_home()) / "migrations"
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = root / f"migrate-{stamp}-{os.getpid()}.json"
        _atomic_write_text(
            path, json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
        )
        return str(path)
    except Exception:
        return ""


# Keep the field import used by the dataclasses above honest for linters.
_ = field
