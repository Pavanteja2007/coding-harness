"""Plugin system (Plugins round, Task C) — bundles of skills, commands,
and optionally custom tool definitions or an MCP server reference.

A plugin is a directory with a manifest (plugin.json):

    {
      "name": "webapp-toolkit",
      "description": "Skills + review command for webapp repos",
      "version": "1.0.0",
      "skills": ["skills/django-conventions"],     # dirs with SKILL.md
      "commands": ["commands/review.md"],           # slash-command templates
      "tools": {"verbs": ["ruff", "ruff check"]},   # BATCH read-only verbs
      "mcp_servers": {"linter": "python -m mcp_server"}  # MCP references
    }

All keys except "name" are optional; paths are plugin-relative. A plugin
WITHOUT a manifest is accepted when it holds skills/ or commands/ subdirs
(implicit layout — everything under skills/ and commands/ is the plugin's
content; name = the directory name). This keeps authoring trivial while
allowing explicit manifests for tool/MCP extensions.

Installed plugins live in ~/.config/neo/plugins/<plugin-name>/ (the
brief's contract). INSTALL copies a LOCAL directory tree (or clones a
git URL) into that location — never a symlink, never a reference to the
source: an installed plugin is self-contained and removable.

Enable/disable: a DISABLED plugin's directory STAYS on disk but every
discovery scan skips it (skills, commands, tool verbs, MCP references).
The state is a `<name>.disabled` marker file beside the directory
(empty file; created by `neo plugin disable`, removed by `neo plugin
enable`). `list_plugins` still LISTS disabled plugins with
enabled=False so `neo plugin list` stays honest about what's installed.

Discovery: installed skills/ and commands/ subdirs are picked up by the
harness's and CLI's existing discovery scans (harness/skills.py and
cli/commands.py both walk ~/.config/neo/plugins/*/skills|commands —
this module's job is install/list/remove plus manifest validation).

Tool extensions: a manifest's "tools": {"verbs": [...]} entries extend
the step loop's BATCH read-only allowlist via harness.tools.
extend_batch_verbs at install time (validated + deny-listed there — a
hostile manifest cannot whitelist destructive verbs). The verbs also
ride Task.config["plugin_tool_verbs"] for explicit per-task control.

MCP server references: "mcp_servers": {<label>: <launch command>} is
RECORDED in the installed manifest and surfaced by `neo plugin list`;
consumption goes through the EXISTING `neo mcp call/list-tools` command
(no new MCP machinery — a plugin points at servers, it doesn't become
one). A registry/marketplace is explicitly out of scope per the
original stretch-list decision.

CLI surface (wired in cli/main.py):
    neo plugin install <local path or git URL>
    neo plugin list
    neo plugin remove <name>
    neo plugin enable <name> / neo plugin disable <name>

Everything is best-effort at the edges: malformed manifests produce
clean usage errors (exit 2), never tracebacks.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "InstallReceipt",
    "PluginError",
    "SkillError",
    "UninstallReport",
    "disable",
    "disable_skill",
    "enable",
    "enable_skill",
    "inspect_plugin",
    "install",
    "install_from_git",
    "install_from_local",
    "install_skill",
    "is_plugin_disabled",
    "list_plugins",
    "list_skill_installs",
    "plugin_state_dir",
    "plugins_root",
    "read_install_receipt",
    "remove",
    "remove_skill",
    "remove_with_report",
    "uninstall",
    "validate_plugin",
]

_NAME_PAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_MANIFEST_BYTES = 64 * 1024

#: Install-state directory under the plugins root. One JSON row per installed
#: plugin, so an uninstall knows what it must also retract and can prove it
#: did. It lives INSIDE the plugins root deliberately: a cleanup that only
#: removes the plugin directory while leaving this behind is the "uninstalled
#: but not really" case this round exists to close.
STATE_DIR_NAME = ".state"
#: Names the staging tree uses. A staging or trash directory left behind is, by
#: construction, an interrupted install, and `neo migrate` reclaims it.
STAGING_PREFIX = ".staging-"
TRASH_PREFIX = ".trash-"
RECEIPT_SCHEMA_VERSION = 1

#: Process-local counter that keeps two staging trees in one process from
#: colliding. Combined with the pid it is unique per install attempt, so an
#: interrupted install leaves a name that identifies WHO and WHEN, which is
#: what makes the residue a diagnosable artifact rather than mystery bytes.
_COUNTER = itertools.count()


class PluginError(Exception):
    """A clean, user-facing plugin error (CLI renders it + exit 2).

    Raised for: bad source paths, unclonable git URLs, malformed
    manifests, unknown-plugin removals, hostile names. The message is
    plain language by contract — the CLI prints it verbatim.
    """


class SkillError(Exception):
    """A clean, user-facing standalone skill management error."""


def plugins_root() -> Path:
    """Return the installed-plugin root.

    ``NEO_PLUGINS_DIR`` is an explicit isolation override; the default
    remains the documented ``~/.config/neo/plugins`` location.
    """
    override = os.environ.get("NEO_PLUGINS_DIR")
    if override:
        return Path(override).expanduser()
    root = os.environ.get("NEO_GLOBAL_ROOT")
    if root:
        return Path(root).expanduser() / "plugins"
    config = os.environ.get("NEO_CONFIG")
    if config:
        return Path(config).expanduser().parent / "plugins"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "neo" / "plugins"
    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / "neo" / "plugins"
    return Path.home() / ".config" / "neo" / "plugins"


def _plugin_dir(name: str) -> Path:
    if not _NAME_PAT.match(name or ""):
        raise PluginError(
            f"invalid plugin name {name!r} (expected letters/digits/dots/"
            "dashes/underscores, starting alphanumeric)"
        )
    return plugins_root() / name


def _disabled_marker(name: str) -> Path:
    """The `<name>.disabled` marker file beside the plugin directory.

    Assumes name already passed _plugin_dir's charset guard (callers go
    through _plugin_dir first) — the marker can never escape the plugins
    root by construction (same guard as the remove path).
    """
    _plugin_dir(name)  # charset guard: raises PluginError on unsafe names
    return plugins_root() / f"{name}.disabled"


def _dir_is_disabled(d: Path) -> bool:
    """True when a plugin directory has its disabled marker beside it."""
    try:
        return (d.parent / f"{d.name}.disabled").is_file()
    except OSError:
        return False


def is_plugin_disabled(name: str) -> bool:
    """True when the named plugin is installed but disabled.

    Assumes name is a plugin name (charset-guarded); unknown names
    report False (never raise — discovery probes must stay total).
    """
    try:
        return _disabled_marker(name).is_file()
    except (PluginError, OSError):
        return False


def read_manifest(plugin_dir: Path) -> Dict[str, Any]:
    """Read plugin.json from a plugin directory; {} when absent.

    Two locations are accepted and the ROOT one wins, so every manifest
    written before ``.claude-plugin/`` existed keeps working byte
    identically: ``<plugin>/plugin.json`` and
    ``<plugin>/.claude-plugin/plugin.json``. The manifest is OPTIONAL - a
    plugin with neither is discovered by convention (see
    ``cli.plugin_runtime.discover_components``), and a plugin with no
    manifest is valid IF it has skills/ or commands/ subdirs (implicit
    layout). Assumes plugin_dir is an existing directory. Raises
    PluginError on an unparseable manifest.
    """
    mf = Path(plugin_dir) / "plugin.json"
    if not mf.is_file():
        alternate = Path(plugin_dir) / ".claude-plugin" / "plugin.json"
        if alternate.is_file():
            mf = alternate
        else:
            return {}
    try:
        if mf.stat().st_size > _MAX_MANIFEST_BYTES:
            raise PluginError(f"manifest {mf} is too large")
        raw = mf.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PluginError(f"cannot read manifest {mf}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PluginError(f"manifest {mf} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginError(f"manifest {mf} must be a JSON object")
    return data


def _contained_manifest_path(source: Path, relative: str) -> Path:
    """Resolve a manifest path and reject traversal or symlink escapes."""
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise PluginError("manifest paths must be non-empty strings")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PluginError(f"manifest path escapes plugin root: {relative!r}")
    root = Path(source).resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PluginError(f"manifest path escapes plugin root: {relative!r}") from exc
    current = root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise PluginError(f"manifest path uses a symlink: {relative!r}")
    return resolved


def _reject_source_symlinks(source: Path) -> None:
    """Reject symlinks anywhere in a plugin tree before copying it."""
    try:
        for entry in source.rglob("*"):
            if entry.is_symlink():
                raise PluginError(f"plugin contains a symlink: {entry.name}")
    except OSError as exc:
        raise PluginError(f"cannot inspect plugin source: {exc}") from exc


def validate_plugin(source: Path, manifest: Dict[str, Any]) -> None:
    """Validate a plugin's shape before install; raise PluginError.

    Checks: name present + safe charset; every listed skills/ entry
    holds a SKILL.md; every listed commands/ entry is a readable .md
    file; tools/mcp_servers are the documented shapes. Unlisted extras
    are allowed (docs, LICENSE, source of tools, etc.).
    """
    source = Path(source) if source is not None else Path(".")
    name = str(manifest.get("name") or source.name)
    if not _NAME_PAT.match(name):
        raise PluginError(
            f"invalid plugin name {name!r} (expected letters/digits/dots/"
            "dashes/underscores, starting alphanumeric)"
        )
    for key in ("skills", "commands"):
        entries = manifest.get(key)
        if entries in (None, []):
            continue
        if not isinstance(entries, list):
            raise PluginError(f"manifest {key!r} must be a list of paths")
        for rel in entries:
            p = _contained_manifest_path(source, rel)
            if key == "skills":
                if not (p / "SKILL.md").is_file():
                    raise PluginError(
                        f"skill entry {rel!r} has no SKILL.md "
                        f"(expected a directory containing SKILL.md)"
                    )
            else:
                if not p.is_file() or p.suffix != ".md":
                    raise PluginError(
                        f"command entry {rel!r} is not a readable .md file"
                    )
    tools = manifest.get("tools")
    if tools is not None:
        if not isinstance(tools, dict) or not isinstance(tools.get("verbs", []), list):
            raise PluginError(
                'manifest "tools" must be an object like {"verbs": [...]}'
            )
        if not all(
            isinstance(verb, str) and verb.strip() for verb in tools.get("verbs", [])
        ):
            raise PluginError(
                'manifest "tools.verbs" entries must be non-empty strings'
            )
    mcp = manifest.get("mcp_servers")
    if mcp is not None:
        if not isinstance(mcp, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip()
            for k, v in mcp.items()
        ):
            raise PluginError(
                'manifest "mcp_servers" must map labels to launch commands'
            )
        for label in mcp:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
                raise PluginError(f"invalid MCP server label in plugin: {label!r}")


def plugin_state_dir() -> Path:
    """Return the install-state directory, creating it on demand.

    Holds one JSON row per installed plugin (see :class:`InstallReceipt`) so an
    uninstall has an inventory to remove and can prove the removal was
    complete. Never raises: an unwritable state directory is reported by the
    install/uninstall receipt, not by an exception from a path helper.
    """
    root = plugins_root() / STATE_DIR_NAME
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


def _receipt_path(name: str) -> Path:
    """Return the install-state row path for one plugin name."""
    _plugin_dir(name)  # charset guard: a receipt can never escape the root
    return plugin_state_dir() / f"{name}.json"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write a JSON document through a unique temp file plus ``os.replace``."""
    import itertools
    import threading

    counter = getattr(_atomic_write_json, "_counter", None)
    if counter is None:
        counter = itertools.count()
        _atomic_write_json._counter = counter  # type: ignore[attr-defined]
    guard = getattr(_atomic_write_json, "_lock", None)
    if guard is None:
        guard = threading.Lock()
        _atomic_write_json._lock = guard  # type: ignore[attr-defined]
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{next(counter)}")
    with guard:
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


def _tree_digest(root: Path) -> Tuple[str, int, int]:
    """Return ``(sha256, file_count, total_bytes)`` for a plugin tree.

    The digest is over sorted ``(relative path, sha256 of bytes)`` pairs, so it
    is stable across directory iteration order and changes when any file's
    content changes. Assumes ``root`` is a real directory that already passed
    the symlink rejection; a file that cannot be read is hashed as
    ``"<unreadable>"`` rather than skipped, so the digest can never claim an
    intact tree it did not read.
    """
    import hashlib

    entries: list = []
    files = 0
    total = 0
    if not root.is_dir():
        return hashlib.sha256(b"").hexdigest(), 0, 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        try:
            data = path.read_bytes()
        except OSError:
            entries.append(f"{rel}\x00<unreadable>")
            files += 1
            continue
        entries.append(f"{rel}\x00{hashlib.sha256(data).hexdigest()}")
        files += 1
        total += len(data)
    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return digest, files, total


@dataclass(frozen=True)
class InstallReceipt:
    """What one plugin install actually put on disk.

    Written to ``<plugins-root>/.state/<name>.json`` and read back by
    :func:`uninstall`, so an uninstall is driven by an inventory rather than by
    a guess about what a plugin may have touched. ``tree_sha256`` is what makes
    an interrupted install detectable: a staged tree that never swapped leaves a
    receipt that does not match anything on disk.
    """

    name: str
    version: str = ""
    source_kind: str = "local"
    source: str = ""
    installed_at: str = ""
    tool_verbs: Tuple[str, ...] = ()
    mcp_servers: Tuple[str, ...] = ()
    commands: Tuple[str, ...] = ()
    skills: Tuple[str, ...] = ()
    tree_sha256: str = ""
    file_count: int = 0
    total_bytes: int = 0
    schema_version: int = RECEIPT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible, secret-free install record."""
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "version": self.version,
            "source_kind": self.source_kind,
            "source": self.source,
            "installed_at": self.installed_at,
            "tool_verbs": list(self.tool_verbs),
            "mcp_servers": list(self.mcp_servers),
            "commands": list(self.commands),
            "skills": list(self.skills),
            "tree_sha256": self.tree_sha256,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }


def read_install_receipt(name: str) -> Optional[InstallReceipt]:
    """Read one install-state row, or ``None`` when absent/unreadable.

    Never raises: a corrupt row is an absent row as far as the caller is
    concerned, and ``uninstall`` reports the leftover it could not read rather
    than refusing to remove the plugin.
    """
    try:
        path = _receipt_path(name)
    except PluginError:
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return InstallReceipt(
        name=str(data.get("name") or name),
        version=str(data.get("version") or ""),
        source_kind=str(data.get("source_kind") or "local"),
        source=str(data.get("source") or ""),
        installed_at=str(data.get("installed_at") or ""),
        tool_verbs=tuple(str(item) for item in (data.get("tool_verbs") or [])),
        mcp_servers=tuple(str(item) for item in (data.get("mcp_servers") or [])),
        commands=tuple(str(item) for item in (data.get("commands") or [])),
        skills=tuple(str(item) for item in (data.get("skills") or [])),
        tree_sha256=str(data.get("tree_sha256") or ""),
        file_count=int(data.get("file_count") or 0),
        total_bytes=int(data.get("total_bytes") or 0),
        schema_version=int(data.get("schema_version") or 0),
    )


def _now_stamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _stage_tree(src: Path, name: str) -> Path:
    """Copy a validated source into a staging tree INSIDE the plugins root.

    Staging inside the root is what makes the swap a rename on one filesystem
    rather than a cross-device copy, which is the difference between an atomic
    swap and a window where a half-written plugin is the live plugin.
    """
    root = plugins_root()
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f"{STAGING_PREFIX}{name}-{os.getpid()}-{next(_COUNTER)}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src, staging, symlinks=False)
    return staging


def _verify_staged(staging: Path, name: str) -> InstallReceipt:
    """Re-validate a STAGED tree and build its install receipt.

    Verification runs against the staged copy, not the source, so a bug in the
    copy (a partial write, a lost file, a manifest that did not survive) is
    caught while the previous version is still the live one.
    """
    try:
        manifest = _implicit_manifest(staging)
        manifest = dict(manifest)
        manifest["name"] = name
        validate_plugin(staging, manifest)
        _reject_source_symlinks(staging)
    except PluginError as exc:
        raise PluginError(f"staged plugin {name!r} failed verification: {exc}") from exc
    digest, files, total = _tree_digest(staging)
    tools = manifest.get("tools") or {}
    verbs = tools.get("verbs") if isinstance(tools, dict) else None
    servers = manifest.get("mcp_servers") or {}
    return InstallReceipt(
        name=name,
        version=str(manifest.get("version") or ""),
        source_kind="local",
        tool_verbs=tuple(str(item) for item in (verbs or [])),
        mcp_servers=tuple(sorted(str(key) for key in servers)),
        commands=tuple(str(item) for item in (manifest.get("commands") or [])),
        skills=tuple(str(item) for item in (manifest.get("skills") or [])),
        tree_sha256=digest,
        file_count=files,
        total_bytes=total,
        installed_at=_now_stamp(),
    )


def _swap_into_place(staging: Path, dest: Path, name: str) -> None:
    """Move the staged tree into place, restoring the previous version on failure.

    The sequence is: move the OLD tree aside into a trash directory, rename the
    staged tree into place, then delete the trash. Each step is a rename on one
    filesystem. If the second rename fails, the old tree is moved back, so a
    failed upgrade leaves the PREVIOUS version installed rather than no plugin.
    """
    trash: Optional[Path] = None
    if dest.is_symlink():
        dest.unlink()
    elif dest.is_dir():
        trash = dest.parent / f"{TRASH_PREFIX}{name}-{os.getpid()}-{next(_COUNTER)}"
        if trash.exists():
            shutil.rmtree(trash, ignore_errors=True)
        os.replace(dest, trash)
    elif dest.exists():
        trash = dest
        dest.unlink()
    try:
        os.replace(staging, dest)
    except OSError as exc:
        if trash is not None and trash.is_dir() and not dest.exists():
            try:
                os.replace(trash, dest)
            except OSError as restore_exc:  # pragma: no cover - needs two failures
                raise PluginError(
                    f"install failed ({exc}) and the previous version could not be "
                    f"restored ({restore_exc}); it is preserved at {trash}"
                ) from restore_exc
        raise PluginError(f"cannot install plugin {name!r}: {exc}") from exc
    if trash is not None:
        shutil.rmtree(trash, ignore_errors=True)


def install_from_local(
    source: str, name_override: Optional[str] = None, *, source_kind: str = "local"
) -> str:
    """Install a plugin from a LOCAL directory ATOMICALLY; returns the name.

    The three phases are explicit and each can fail without touching the live
    plugin: **stage** into ``<plugins-root>/.staging-<name>-*``, **verify** the
    staged copy (manifest re-read, shape re-validated, symlinks re-rejected,
    tree digest recorded), then **swap** it into place with renames, restoring
    the previous version if the swap fails. A copy that dies half-way leaves
    the previous version installed — that is the property the old
    ``rmtree`` + ``copytree`` install could not offer.

    Assumes source is an existing local directory (the operator's own machine,
    the same trust boundary as any source checkout they could run anyway).
    Raises :class:`PluginError` on any bad shape; never touches anything outside
    the plugins root.
    """
    src = Path(source).expanduser().resolve()
    if not src.is_dir():
        raise PluginError(f"plugin source is not a directory: {source}")
    manifest = _implicit_manifest(src)
    if name_override:
        manifest = dict(manifest)
        manifest["name"] = name_override
    name = str(manifest.get("name") or "")
    # Two ADDITIVE passes around the unchanged one in the middle. The
    # teaching pass runs first because its messages name the fix; the
    # manifest-identity pass runs LAST so it can never pre-empt a
    # traversal, symlink or MCP-label refusal that has always fired.
    from cli import plugin_runtime as _runtime

    _runtime.validate_teaching(src, manifest)
    validate_plugin(src, manifest)
    _runtime.validate_manifest_identity(src, manifest)
    _reject_source_symlinks(src)
    dest = _plugin_dir(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = _stage_tree(src, name)
    try:
        receipt = _verify_staged(staging, name)
        receipt = InstallReceipt(
            **{
                **receipt.to_dict(),
                "name": name,
                "source_kind": source_kind,
                "source": str(src),
                "tree_sha256": receipt.tree_sha256,
                "file_count": receipt.file_count,
                "total_bytes": receipt.total_bytes,
                "tool_verbs": receipt.tool_verbs,
                "mcp_servers": receipt.mcp_servers,
                "commands": receipt.commands,
                "skills": receipt.skills,
                "version": receipt.version,
            }
        )
        _swap_into_place(staging, dest, name)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    try:
        _atomic_write_json(_receipt_path(name), receipt.to_dict())
    except OSError as exc:
        # The plugin IS installed; the missing receipt only degrades what an
        # uninstall can prove, so it is reported without failing the install.
        receipt = InstallReceipt(
            **{
                **receipt.to_dict(),
                "tree_sha256": receipt.tree_sha256,
                "file_count": receipt.file_count,
                "total_bytes": receipt.total_bytes,
                "tool_verbs": receipt.tool_verbs,
                "mcp_servers": receipt.mcp_servers,
                "commands": receipt.commands,
                "skills": receipt.skills,
                "version": receipt.version,
            }
        )
        raise PluginError(
            f"plugin {name!r} installed but its install record could not be "
            f"written ({exc}); `neo migrate` will backfill it"
        ) from exc
    # A fresh install is always enabled: a stale marker from a
    # previously-disabled install must not silently mute the upgrade.
    try:
        (dest.parent / f"{dest.name}.disabled").unlink(missing_ok=True)
    except OSError:
        pass
    return name


def install_from_git(url: str, name_override: Optional[str] = None) -> str:
    """Install a plugin from a GIT URL; returns the installed name.

    Clones with --depth 1 (plugins are content, not history) into a
    temp dir, then installs via the local path. Assumes a plain https
    git URL; the temp clone is always cleaned up. Raises PluginError
    on clone failure with git's own message surfaced.
    """
    if not re.match(r"^(https?|ssh|git)://", url or "") and not url.endswith(".git"):
        raise PluginError(
            f"not a git URL: {url!r} (expected https://host/path[.git] "
            "or a local directory path)"
        )
    with tempfile.TemporaryDirectory(prefix="neo-plugin-") as td:
        clone_dir = Path(td) / "src"
        try:
            cp = subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", url, str(clone_dir)],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PluginError(f"git clone failed: {type(exc).__name__}") from exc
        if cp.returncode != 0 or not clone_dir.is_dir():
            msg = (cp.stderr or cp.stdout or "").strip().splitlines()
            raise PluginError(
                "git clone failed: " + (msg[-1] if msg else f"exit {cp.returncode}")
            )
        return install_from_local(
            str(clone_dir), name_override=name_override, source_kind="git"
        )


def install(source: str, name_override: Optional[str] = None) -> str:
    """Install from a local path OR git URL (dispatches on shape)."""
    if re.match(r"^(https?|ssh|git)://", source or "") or source.endswith(".git"):
        return install_from_git(source, name_override=name_override)
    return install_from_local(source, name_override=name_override)


def read_plugin_manifest(plugin_dir: Path) -> Tuple[Dict[str, Any], str, bool]:
    """Return ``(manifest, path, present)`` for one plugin directory.

    Reads ``plugin.json`` first and ``.claude-plugin/plugin.json`` second,
    so the historical root manifest keeps working byte-identically. Both
    locations go through ``cli.plugins.read_manifest``'s own size and
    JSON guards rather than a second parser.
    """
    root = Path(plugin_dir).expanduser()
    for relative in ("plugin.json", ".claude-plugin/plugin.json"):
        if (root / relative).is_file():
            return read_manifest(root), str(root / relative), True
    return {}, "", False


def _implicit_manifest(source: Path) -> Dict[str, Any]:
    """Build a manifest for a manifest-less plugin by CONVENTION.

    Every component kind the loader recognises at the plugin ROOT is folded
    into the synthetic manifest: ``skills/``, ``commands/``, ``agents/``,
    ``hooks/hooks.json``, ``.mcp.json``, ``.lsp.json``,
    ``monitors/monitors.json``, ``bin/`` and ``settings.json``. A plugin
    with no manifest but a ``.mcp.json`` or an ``agents/`` directory is
    therefore a real plugin, not an unrecognised directory - the discovery
    lives in ``cli.plugin_runtime.component_paths`` and is imported LAZILY
    so the two modules cannot form an import cycle.

    The name is the DIRECTORY name, which is the documented rule rather than
    a fallback.
    """
    if read_plugin_manifest(source)[2]:
        return read_manifest(source)
    from cli import plugin_runtime as _runtime

    found = _runtime.component_paths(source)
    if not any(found.get(kind) for kind in found):
        # A component built inside ``.claude-plugin/`` is the cause of most
        # "my plugin contributes nothing" reports, so the refusal is the
        # TEACHING one rather than "not a recognizable plugin".
        mistake = _runtime.metadata_dir_mistake(source)
        if mistake:
            raise PluginError(mistake)
        raise PluginError(
            f"{source} has no plugin.json and none of the conventional "
            "component locations (skills/, commands/, agents/, hooks/hooks.json, "
            ".mcp.json, .lsp.json, monitors/monitors.json, bin/, settings.json) "
            "— not a recognizable plugin"
        )
    # NO ``version`` key: a manifest-less plugin has DECLARED no version, and
    # inventing "0.0.0" would make ``/plugin inspect`` claim a version the
    # author never wrote. It stays absent and inspect reports "unknown".
    manifest: Dict[str, Any] = {"name": source.name}
    for kind in ("skills", "commands", "agents"):
        if found.get(kind):
            manifest[kind] = list(found[kind])
    if found.get("mcp"):
        document: Dict[str, Any] = {}
        try:
            raw = json.loads((source / ".mcp.json").read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                document = raw.get("mcpServers") or raw.get("servers") or {}
        except (OSError, ValueError):
            document = {}
        servers = {}
        for label, spec in (document or {}).items():
            if not isinstance(label, str) or not _NAME_PAT.match(label):
                continue
            servers[label] = (
                str(spec.get("command") or spec.get("url") or "")
                if isinstance(spec, dict)
                else str(spec)
            )
        if servers:
            manifest["mcp_servers"] = servers
    return manifest


def _installed_manifest_name(dest: Path) -> str:
    """The installed plugin's effective name (manifest name, else dir)."""
    mf = read_manifest(dest)
    return str(mf.get("name") or dest.name)


def list_plugins() -> List[Dict[str, Any]]:
    """Installed plugins as [{name, dir, description, skills, commands,
    tools, mcp_servers, enabled}] — what `neo plugin list` renders.
    Malformed entries are skipped with their error attached (never a
    traceback; the listing must stay honest about a broken install).
    Disabled plugins are STILL listed (enabled=False) so the listing
    stays honest about what's on disk; every DISCOVERY consumer
    (skills/commands/tool verbs/MCP labels) skips them.
    """
    root = plugins_root()
    out: List[Dict[str, Any]] = []
    if not root.is_dir():
        return out
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for d in entries:
        try:
            if not d.is_dir() or d.name.startswith("."):
                continue
        except OSError:
            continue
        try:
            mf = read_manifest(d)
        except PluginError as exc:
            out.append(
                {
                    "name": d.name,
                    "dir": str(d),
                    "error": str(exc),
                    "enabled": not _dir_is_disabled(d),
                    "origin": f"plugin:{d.name}",
                }
            )
            continue
        entry = {
            "name": str(mf.get("name") or d.name),
            "dir": str(d),
            "description": str(mf.get("description") or ""),
            "skills": [str(s) for s in (mf.get("skills") or [])],
            "commands": [str(c) for c in (mf.get("commands") or [])],
            "tools": mf.get("tools") or {},
            "mcp_servers": mf.get("mcp_servers") or {},
            "enabled": not _dir_is_disabled(d),
            "origin": f"plugin:{(mf.get('name') or d.name)!s}",
        }
        try:
            validate_plugin(d, mf)
        except PluginError as exc:
            entry["error"] = str(exc)
        # count what's ACTUALLY on disk (an honest listing — a manifest
        # that lists entries a later edit deleted shows the truth)
        on_disk_skills = (
            sorted(p.parent.name for p in (d / "skills").glob("*/SKILL.md"))
            if (d / "skills").is_dir()
            else []
        )
        on_disk_cmds = (
            sorted(p.stem for p in (d / "commands").glob("*.md"))
            if (d / "commands").is_dir()
            else []
        )
        entry["skills_on_disk"] = on_disk_skills
        entry["commands_on_disk"] = on_disk_cmds
        out.append(entry)
    return out


def inspect_plugin(name: str) -> Optional[Dict[str, Any]]:
    """Return one installed plugin entry, or ``None`` when absent."""
    for entry in list_plugins():
        if entry.get("name") == name:
            return entry
    try:
        wanted = _plugin_dir(name).name
    except PluginError:
        return None
    for entry in list_plugins():
        if Path(str(entry.get("dir", ""))).name == wanted:
            return entry
    return None


@dataclass(frozen=True)
class UninstallReport:
    """What an uninstall actually removed, and whether that was everything.

    "Uninstalled" used to mean "the plugin directory went away", which left the
    install-state row behind and let a re-install inherit a stale record. This
    is the receipt that closes it: it enumerates the four categories the
    operation is responsible for — **files** (the plugin tree), **database
    rows** (the install-state record), **logs** (staging/trash residue from an
    interrupted install), and **path entries** (the surface the plugin
    registered: batch verbs and MCP labels) — and then RE-SCANS the plugins
    root so ``complete`` is a measurement rather than a claim.

    ``os_path_modified`` is always ``False`` and carries the reason: a plugin
    install never edits the OS ``PATH``, so an uninstall that reported touching
    it would be reporting something that did not happen. The category is kept
    explicitly so a reader can see it was considered.
    """

    name: str
    removed_files: int = 0
    removed_bytes: int = 0
    removed_tree: bool = False
    removed_marker: bool = False
    removed_rows: int = 0
    removed_logs: int = 0
    retracted_path_entries: Tuple[str, ...] = ()
    os_path_modified: bool = False
    os_path_note: str = (
        "a plugin install never modifies the OS PATH, so an uninstall has "
        "nothing to revert there"
    )
    remaining: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()
    receipt_found: bool = False

    @property
    def complete(self) -> bool:
        """Whether nothing named after this plugin remains under the root."""
        return not self.remaining

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible uninstall record."""
        return {
            "name": self.name,
            "complete": self.complete,
            "removed_files": self.removed_files,
            "removed_bytes": self.removed_bytes,
            "removed_tree": self.removed_tree,
            "removed_marker": self.removed_marker,
            "removed_rows": self.removed_rows,
            "removed_logs": self.removed_logs,
            "retracted_path_entries": list(self.retracted_path_entries),
            "os_path_modified": self.os_path_modified,
            "os_path_note": self.os_path_note,
            "receipt_found": self.receipt_found,
            "remaining": list(self.remaining),
            "errors": list(self.errors),
        }


def _residue_for(name: str) -> List[Path]:
    """Return staging/trash residue belonging to one plugin name."""
    root = plugins_root()
    if not root.is_dir():
        return []
    out: List[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.name.startswith((STAGING_PREFIX, TRASH_PREFIX)):
            continue
        tail = entry.name
        for prefix in (STAGING_PREFIX, TRASH_PREFIX):
            if tail.startswith(prefix):
                tail = tail[len(prefix) :]
                break
        if tail.split("-", 1)[0] == name:
            out.append(entry)
    return out


def _rescan_remaining(name: str) -> List[str]:
    """Return every path under the plugins root that still names this plugin.

    This is the completeness proof. It walks the root (not the known paths) so
    a stray file, a stale temp directory, or a state row written by an older
    version is still found and reported instead of being assumed gone.
    """
    root = plugins_root()
    if not root.is_dir():
        return []
    found: List[str] = []
    needle = name.casefold()
    try:
        for entry in root.rglob("*"):
            rel = entry.relative_to(root).as_posix()
            if entry.name.casefold() == needle or needle in rel.casefold():
                found.append(rel)
    except OSError:
        return sorted(found)
    return sorted(found)


def uninstall(name: str) -> UninstallReport:
    """Remove EVERYTHING an install of this plugin created, and prove it.

    Covers all four categories, in this order so the tree digest can be taken
    while the tree still exists:

    1. **files** — the plugin directory (counted by the install receipt's
       ``file_count``/``total_bytes`` when available, measured otherwise).
    2. **database rows** — the ``.state/<name>.json`` install record.
    3. **logs** — the ``<name>.disabled`` marker and any staging/trash residue
       from an interrupted install.
    4. **path entries** — the batch tool verbs and MCP labels the plugin
       registered, read from the install record BEFORE it is deleted and
       reported as retracted. The OS ``PATH`` is explicitly not touched; see
       :class:`UninstallReport`.

    Then it re-scans the whole plugins root. Anything still named after the
    plugin is listed in ``remaining`` and ``complete`` is ``False`` — a partial
    removal is reported as a partial removal, never as a success.
    """
    d = _plugin_dir(name)  # raises PluginError on unsafe names
    plugins_root()
    receipt = read_install_receipt(name)
    if not d.is_dir() and receipt is None and not _residue_for(name):
        raise PluginError(f"no installed plugin named {name!r}")
    errors: List[str] = []
    removed_files = 0
    removed_bytes = 0
    removed_tree = False
    if d.is_dir():
        try:
            _, files, total = _tree_digest(d)
        except OSError:
            files, total = 0, 0
        try:
            shutil.rmtree(d)
            removed_tree = True
            removed_files = files
            removed_bytes = total
        except OSError as exc:
            errors.append(f"could not remove {d}: {exc}")
    elif d.is_symlink():
        try:
            d.unlink()
            removed_tree = True
        except OSError as exc:
            errors.append(f"could not remove symlink {d}: {exc}")
    removed_marker = False
    marker = _disabled_marker(name)
    if marker.is_file():
        try:
            marker.unlink()
            removed_marker = True
        except OSError as exc:
            errors.append(f"could not remove {marker}: {exc}")
    removed_logs = 0
    for residue in _residue_for(name):
        removed_logs += 1
        try:
            if residue.is_dir():
                shutil.rmtree(residue)
            else:
                residue.unlink()
        except OSError as exc:
            errors.append(f"could not remove {residue}: {exc}")
    removed_rows = 0
    receipt_path = _receipt_path(name)
    if receipt_path.is_file():
        try:
            receipt_path.unlink()
            removed_rows = 1
        except OSError as exc:
            errors.append(f"could not remove {receipt_path}: {exc}")
    retracted: List[str] = []
    if receipt is not None:
        retracted.extend(f"batch-verb:{verb}" for verb in receipt.tool_verbs)
        retracted.extend(f"mcp-label:{label}" for label in receipt.mcp_servers)
    remaining = _rescan_remaining(name)
    return UninstallReport(
        name=name,
        removed_files=removed_files,
        removed_bytes=removed_bytes,
        removed_tree=removed_tree,
        removed_marker=removed_marker,
        removed_rows=removed_rows,
        removed_logs=removed_logs,
        retracted_path_entries=tuple(retracted),
        remaining=tuple(remaining),
        errors=tuple(errors),
        receipt_found=receipt is not None,
    )


def remove_with_report(name: str) -> UninstallReport:
    """Uninstall a plugin and return the full :class:`UninstallReport`."""
    return uninstall(name)


def remove(name: str) -> str:
    """Remove an installed plugin COMPLETELY; returns the removed name.

    Keeps its historical signature and return value. It now delegates to
    :func:`uninstall`, so it removes the install-state row and any interrupted
    install residue as well as the directory, and it **raises** when the
    re-scan found something still named after the plugin — a partial removal
    must not be reported as a successful removal. The report is attached to the
    raised error so the operator sees what survived.
    """
    report = uninstall(name)
    if not report.complete:
        raise PluginError(
            f"plugin {name!r} was only partially removed; these remain under "
            f"{plugins_root()}: {list(report.remaining)}"
        )
    return name


def disable(name: str) -> str:
    """Disable an installed plugin; returns the disabled name.

    The directory STAYS (re-enable with `enable`); a `<name>.disabled`
    marker beside it makes every discovery scan skip it. Idempotent
    (disabling twice is a no-op). Raises PluginError when the plugin
    isn't installed or the name is unsafe.
    """
    d = _plugin_dir(name)  # raises PluginError on unsafe names
    if not d.is_dir():
        raise PluginError(f"no installed plugin named {name!r}")
    try:
        _disabled_marker(name).write_text("disabled\n", encoding="utf-8")
    except OSError as exc:
        raise PluginError(f"cannot disable plugin {name!r}: {exc}") from exc
    return name


def enable(name: str) -> str:
    """Re-enable a disabled plugin; returns the enabled name.

    Removes the `<name>.disabled` marker so discovery scans pick the
    plugin up again. Enabling an already-enabled plugin is a no-op.
    Raises PluginError when the plugin isn't installed or the name is
    unsafe.
    """
    d = _plugin_dir(name)  # raises PluginError on unsafe names
    if not d.is_dir():
        raise PluginError(f"no installed plugin named {name!r}")
    try:
        _disabled_marker(name).unlink(missing_ok=True)
    except OSError as exc:
        raise PluginError(f"cannot enable plugin {name!r}: {exc}") from exc
    return name


def apply_tool_extensions() -> List[str]:
    """Feed every installed plugin's tool verbs to the harness BATCH
    allowlist; returns the verbs applied. Idempotent (extend_batch_verbs
    dedupes); a broken/missing plugin is skipped, never raised — tool
    extension is best-effort by design.

    Called lazily by the CLI (fix/interactive sessions) and safe to
    call repeatedly; plugins without a "tools" manifest key contribute
    nothing.
    """
    applied: List[str] = []
    for entry in list_plugins():
        if entry.get("error"):
            continue
        if entry.get("enabled") is False:
            continue  # disabled plugins contribute nothing
        tools = entry.get("tools") or {}
        verbs = tools.get("verbs") if isinstance(tools, dict) else None
        if isinstance(verbs, list):
            safe = [v for v in verbs if isinstance(v, str)]
            try:
                from harness.tools import extend_batch_verbs

                extend_batch_verbs(safe)
            except Exception:
                continue
            applied.extend(safe)
    return applied


def config_tool_verbs() -> List[str]:
    """All installed plugins' tool verbs as a Task.config
    "plugin_tool_verbs" value (see harness/config.py). Empty list when
    no plugin extends tools."""
    verbs: List[str] = []
    for entry in list_plugins():
        if entry.get("error"):
            continue
        if entry.get("enabled") is False:
            continue
        tools = entry.get("tools") or {}
        values = tools.get("verbs") if isinstance(tools, dict) else None
        if isinstance(values, list):
            verbs.extend(value for value in values if isinstance(value, str))
    return verbs


def _standalone_skills_root(tier: str, repo_path: Optional[str] = None) -> Path:
    if tier == "global":
        return plugins_root().parent / "skills"
    if tier != "project":
        raise SkillError(f"unknown skill tier: {tier!r}")
    if repo_path:
        candidate = Path(repo_path).expanduser()
        repo = candidate.parent if candidate.name == ".neo" else candidate
    else:
        try:
            from cli.neoconfig import project_settings_dir

            project_dir = project_settings_dir()
        except Exception as exc:
            raise SkillError(f"cannot resolve project skills root: {exc}") from exc
        if project_dir is None:
            raise SkillError(
                "no .neo project directory found; pass --repo or run inside a project"
            )
        repo = project_dir.parent
    return repo / ".neo" / "skills"


def _read_skill_document(path: Path) -> Dict[str, str]:
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            raise SkillError(f"skill file is unsafe or too large: {path}")
        raw = path.read_bytes()
    except OSError as exc:
        raise SkillError(f"cannot read skill file {path}: {exc}") from exc
    if b"\x00" in raw:
        raise SkillError(f"skill file is not text: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError(f"skill file is not UTF-8: {path}") from exc
    lines = text.splitlines()
    meta: Dict[str, str] = {}
    body_start = 0
    if lines and lines[0].strip() == "---":
        closing = None
        for index, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                closing = index
                break
            match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
            if match:
                meta[match.group(1).lower()] = match.group(2).strip().strip("\"'")
        if closing is None:
            raise SkillError(f"skill frontmatter is not closed: {path}")
        body_start = closing + 1
    if not "\n".join(lines[body_start:]).strip():
        raise SkillError(f"skill body is empty: {path}")
    return {
        "name": meta.get("name") or path.parent.name,
        "description": meta.get("description", ""),
    }


def install_skill(
    source: str,
    tier: str = "global",
    repo_path: Optional[str] = None,
    name_override: Optional[str] = None,
) -> str:
    """Install or replace one standalone skill from a local directory/file."""
    src = Path(source).expanduser().resolve()
    src_file = src / "SKILL.md" if src.is_dir() else src
    if not src_file.is_file():
        raise SkillError(f"skill source does not contain SKILL.md: {source}")
    if src.is_dir():
        _reject_source_symlinks(src)
    else:
        current = src_file
        while True:
            if current.is_symlink():
                raise SkillError(f"skill source uses a symlink: {src_file}")
            if current.parent == current:
                break
            current = current.parent
    document = _read_skill_document(src_file)
    name = name_override or document["name"]
    if not _NAME_PAT.fullmatch(name):
        raise SkillError(f"invalid skill name {name!r}")
    root = _standalone_skills_root(tier, repo_path)
    if root.is_symlink():
        raise SkillError(f"refusing to write through symlinked skills root: {root}")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / name
    if destination.is_symlink():
        raise SkillError(f"refusing to replace symlinked skill: {destination}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=root))
    try:
        if src.is_dir():
            shutil.copytree(src, temporary / name, dirs_exist_ok=True, symlinks=False)
        else:
            (temporary / name).mkdir()
            shutil.copy2(src_file, temporary / name / "SKILL.md")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(temporary / name), str(destination))
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    (destination / "SKILL.md.disabled").unlink(missing_ok=True)
    return name


def _skill_entries_for_root(root: Path, origin: str) -> List[Dict[str, Any]]:
    if not root.is_dir() or root.is_symlink():
        return []
    out: List[Dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        active = entry / "SKILL.md"
        disabled = entry / "SKILL.md.disabled"
        path = active if active.is_file() else disabled
        if not entry.is_dir() or entry.is_symlink() or not path.is_file():
            continue
        try:
            document = _read_skill_document(path)
        except SkillError:
            out.append(
                {
                    "name": entry.name,
                    "origin": origin,
                    "source": str(path),
                    "enabled": False,
                    "error": "invalid skill file",
                }
            )
            continue
        out.append(
            {
                "name": document["name"],
                "description": document["description"],
                "origin": origin,
                "source": str(path),
                "enabled": active.is_file(),
            }
        )
    return out


def list_skill_installs(repo_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """List standalone and plugin skills with origin, state, and source."""
    out: List[Dict[str, Any]] = []
    for tier in ("project", "global"):
        try:
            out.extend(
                _skill_entries_for_root(_standalone_skills_root(tier, repo_path), tier)
            )
        except SkillError:
            continue
    winners: set[tuple[str, str]] = set()
    try:
        from harness.skills import discover_skills

        known = {(item.get("origin"), item.get("name")) for item in out}
        for skill in discover_skills(repo_path=repo_path):
            winners.add((skill.origin, skill.name))
            if (skill.origin, skill.name) in known:
                continue
            out.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "origin": skill.origin,
                    "source": skill.source,
                    "enabled": True,
                }
            )
    except Exception:
        pass
    for item in out:
        item["active"] = (str(item.get("origin")), str(item.get("name"))) in winners
    return sorted(
        out, key=lambda item: (str(item.get("name", "")), str(item.get("origin", "")))
    )


def _find_standalone_skill(
    name: str, tier: str, repo_path: Optional[str] = None
) -> Path:
    if not _NAME_PAT.fullmatch(name or ""):
        raise SkillError(f"invalid skill name {name!r}")
    root = _standalone_skills_root(tier, repo_path)
    direct = root / name
    if direct.is_dir() and not direct.is_symlink():
        return direct
    if not root.is_dir() or root.is_symlink():
        raise SkillError(f"no standalone skill named {name!r} in {tier} tier")
    for entry in sorted(root.iterdir()):
        for filename in ("SKILL.md", "SKILL.md.disabled"):
            path = entry / filename
            if not path.is_file():
                continue
            try:
                if _read_skill_document(path)["name"] == name:
                    return entry
            except SkillError:
                continue
    raise SkillError(f"no standalone skill named {name!r} in {tier} tier")


def remove_skill(
    name: str, tier: str = "global", repo_path: Optional[str] = None
) -> str:
    """Remove one standalone skill from the selected tier."""
    directory = _find_standalone_skill(name, tier, repo_path)
    if directory.is_symlink():
        raise SkillError(f"refusing to remove symlinked skill: {directory}")
    shutil.rmtree(directory)
    return name


def disable_skill(
    name: str, tier: str = "global", repo_path: Optional[str] = None
) -> str:
    """Disable one standalone skill without deleting its body."""
    directory = _find_standalone_skill(name, tier, repo_path)
    active = directory / "SKILL.md"
    disabled = directory / "SKILL.md.disabled"
    if active.is_file() and not disabled.exists():
        active.replace(disabled)
    return name


def enable_skill(
    name: str, tier: str = "global", repo_path: Optional[str] = None
) -> str:
    """Re-enable one standalone skill."""
    directory = _find_standalone_skill(name, tier, repo_path)
    active = directory / "SKILL.md"
    disabled = directory / "SKILL.md.disabled"
    if disabled.is_file() and not active.exists():
        disabled.replace(active)
    return name
