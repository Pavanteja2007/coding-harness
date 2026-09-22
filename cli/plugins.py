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

Installed plugins live in ~/.config/vex/plugins/<plugin-name>/ (the
brief's contract). INSTALL copies a LOCAL directory tree (or clones a
git URL) into that location — never a symlink, never a reference to the
source: an installed plugin is self-contained and removable.

Enable/disable: a DISABLED plugin's directory STAYS on disk but every
discovery scan skips it (skills, commands, tool verbs, MCP references).
The state is a `<name>.disabled` marker file beside the directory
(empty file; created by `vex plugin disable`, removed by `vex plugin
enable`). `list_plugins` still LISTS disabled plugins with
enabled=False so `vex plugin list` stays honest about what's installed.

Discovery: installed skills/ and commands/ subdirs are picked up by the
harness's and CLI's existing discovery scans (harness/skills.py and
cli/commands.py both walk ~/.config/vex/plugins/*/skills|commands —
this module's job is install/list/remove plus manifest validation).

Tool extensions: a manifest's "tools": {"verbs": [...]} entries extend
the step loop's BATCH read-only allowlist via harness.tools.
extend_batch_verbs at install time (validated + deny-listed there — a
hostile manifest cannot whitelist destructive verbs). The verbs also
ride Task.config["plugin_tool_verbs"] for explicit per-task control.

MCP server references: "mcp_servers": {<label>: <launch command>} is
RECORDED in the installed manifest and surfaced by `vex plugin list`;
consumption goes through the EXISTING `vex mcp call/list-tools` command
(no new MCP machinery — a plugin points at servers, it doesn't become
one). A registry/marketplace is explicitly out of scope per the
original stretch-list decision.

CLI surface (wired in cli/main.py):
    vex plugin install <local path or git URL>
    vex plugin list
    vex plugin remove <name>
    vex plugin enable <name> / vex plugin disable <name>

Everything is best-effort at the edges: malformed manifests produce
clean usage errors (exit 2), never tracebacks.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "PluginError",
    "disable",
    "enable",
    "install",
    "install_from_git",
    "install_from_local",
    "is_plugin_disabled",
    "list_plugins",
    "plugins_root",
    "read_manifest",
    "remove",
    "validate_plugin",
]

_NAME_PAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_MANIFEST_BYTES = 64 * 1024


class PluginError(Exception):
    """A clean, user-facing plugin error (CLI renders it + exit 2).

    Raised for: bad source paths, unclonable git URLs, malformed
    manifests, unknown-plugin removals, hostile names. The message is
    plain language by contract — the CLI prints it verbatim.
    """


def plugins_root() -> Path:
    """~/.config/vex/plugins — the installed-plugin location contract."""
    return Path.home() / ".config" / "vex" / "plugins"


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

    A plugin with no manifest is valid IF it has skills/ or commands/
    subdirs (implicit layout). Assumes plugin_dir is an existing
    directory. Raises PluginError on an unparseable manifest.
    """
    mf = plugin_dir / "plugin.json"
    if not mf.is_file():
        return {}
    try:
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


def validate_plugin(source: Path, manifest: Dict[str, Any]) -> None:
    """Validate a plugin's shape before install; raise PluginError.

    Checks: name present + safe charset; every listed skills/ entry
    holds a SKILL.md; every listed commands/ entry is a readable .md
    file; tools/mcp_servers are the documented shapes. Unlisted extras
    are allowed (docs, LICENSE, source of tools, etc.).
    """
    name = str(manifest.get("name") or (source.name if source else ""))
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
            if not isinstance(rel, str) or not rel:
                raise PluginError(f"manifest {key!r} entries must be strings")
            p = source / rel
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
    if tools is not None and (
        not isinstance(tools, dict) or not isinstance(tools.get("verbs", []), list)
    ):
        raise PluginError('manifest "tools" must be an object like {"verbs": [...]}')
    mcp = manifest.get("mcp_servers")
    if mcp is not None and not (
        isinstance(mcp, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in mcp.items())
    ):
        raise PluginError('manifest "mcp_servers" must map labels to launch commands')


def _implicit_manifest(source: Path) -> Dict[str, Any]:
    """Build a manifest for a manifest-less plugin: everything under
    skills/ and commands/ is content; name = directory name."""
    if (source / "plugin.json").is_file():
        return read_manifest(source)
    has_skills = (source / "skills").is_dir()
    has_commands = (source / "commands").is_dir()
    if not has_skills and not has_commands:
        raise PluginError(
            f"{source} has no plugin.json and no skills/ or commands/ "
            "subdirectory — not a recognizable plugin"
        )
    manifest: Dict[str, Any] = {"name": source.name}
    if has_skills:
        manifest["skills"] = sorted(
            f"skills/{d.name}" for d in (source / "skills").iterdir() if d.is_dir()
        )
    if has_commands:
        manifest["commands"] = sorted(
            f"commands/{f.name}" for f in (source / "commands").glob("*.md")
        )
    return manifest


def _installed_manifest_name(dest: Path) -> str:
    """The installed plugin's effective name (manifest name, else dir)."""
    mf = read_manifest(dest)
    return str(mf.get("name") or dest.name)


def install_from_local(source: str, name_override: Optional[str] = None) -> str:
    """Install a plugin from a LOCAL directory; returns the installed name.

    Assumes source is an existing local directory (the operator's own
    machine — local trust boundary, same as any source checkout they
    could run anyway). Copies the whole tree into
    ~/.config/vex/plugins/<name>/; an existing plugin of the same name
    is REPLACED (re-install/upgrade path). Raises PluginError on any
    bad shape; never touches anything outside the plugins root.
    """
    src = Path(source).expanduser().resolve()
    if not src.is_dir():
        raise PluginError(f"plugin source is not a directory: {source}")
    manifest = _implicit_manifest(src)
    if name_override:
        manifest = dict(manifest)
        manifest["name"] = name_override
    name = str(manifest.get("name") or "")
    validate_plugin(src, manifest)
    dest = _plugin_dir(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)  # replace = upgrade
    shutil.copytree(src, dest)
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
    with tempfile.TemporaryDirectory(prefix="vex-plugin-") as td:
        clone_dir = Path(td) / "src"
        cp = subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", url, str(clone_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if cp.returncode != 0 or not clone_dir.is_dir():
            msg = (cp.stderr or cp.stdout or "").strip().splitlines()
            raise PluginError(
                "git clone failed: " + (msg[-1] if msg else f"exit {cp.returncode}")
            )
        return install_from_local(str(clone_dir), name_override=name_override)


def install(source: str, name_override: Optional[str] = None) -> str:
    """Install from a local path OR git URL (dispatches on shape)."""
    if re.match(r"^(https?|ssh|git)://", source or "") or source.endswith(".git"):
        return install_from_git(source, name_override=name_override)
    return install_from_local(source, name_override=name_override)


def list_plugins() -> List[Dict[str, Any]]:
    """Installed plugins as [{name, dir, description, skills, commands,
    tools, mcp_servers, enabled}] — what `vex plugin list` renders.
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
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
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
        }
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


def remove(name: str) -> str:
    """Remove an installed plugin by name; returns the removed name.

    Raises PluginError when the plugin isn't installed (or the name is
    unsafe — the remove path must never rmtree outside the plugins root;
    _plugin_dir's charset guard enforces that). A stale disabled marker
    is removed alongside the directory.
    """
    d = _plugin_dir(name)  # raises PluginError on unsafe names
    if not d.is_dir():
        raise PluginError(f"no installed plugin named {name!r}")
    shutil.rmtree(d)
    try:
        _disabled_marker(name).unlink(missing_ok=True)
    except OSError:
        pass
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
            continue  # disabled plugins contribute nothing
        tools = entry.get("tools") or {}
        v = tools.get("verbs") if isinstance(tools, dict) else None
        if isinstance(v, list):
            verbs.extend(x for x in v if isinstance(x, str))
    return verbs
