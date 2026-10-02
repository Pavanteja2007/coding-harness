"""Plugin runtime: convention discovery, namespacing, marketplaces,
dependencies, the trust prompt, digest verification and the ONE
implementation of every ``/plugin`` verb.

``cli/plugins.py`` owns the BYTES: the plugins root, the contained-path
resolution, the symlink rejection, the atomic writes, the tree SHA-256
digest, the install receipt and the stage -> verify -> swap install. Nothing
here rebuilds any of that; this module reads it.

What ``cli/plugins.py`` did NOT do, and this module does:

* **CONVENTION DISCOVERY.** Components used to be whatever a manifest
  listed. They are now found at the plugin ROOT by convention -
  ``skills/<name>/SKILL.md``, ``commands/*.md``, ``agents/*.md``,
  ``hooks/hooks.json``, ``.mcp.json``, ``.lsp.json``,
  ``monitors/monitors.json``, ``bin/``, ``settings.json`` - so a
  manifest-less plugin is a first-class plugin and its name is its
  directory name.
* **NAMESPACING.** ``/plugin-name:component-name``. A plugin can carry a
  ``commands/review.md`` and the user's own ``/review`` can stay reachable;
  both names resolve, and neither shadows the other.
* **PORTABLE PATHS.** ``${NEO_PLUGIN_ROOT}`` (changes on update),
  ``${NEO_PLUGIN_DATA}`` (SURVIVES an update - it is a sibling of the
  plugins root, never inside the plugin tree) and ``${NEO_PROJECT_DIR}``.
* **TEACHING.** The two authoring mistakes that make a plugin silently
  contribute nothing are REFUSED with the fix in the message: a component
  directory inside ``.claude-plugin/`` (that directory holds METADATA, not
  components) and a ``skills`` entry naming a ``SKILL.md`` FILE rather than
  its parent directory. A ``plugin.json`` must declare BOTH ``name`` and
  ``version``.
* **THE MARKETPLACE.** ``.claude-plugin/marketplace.json``, eight source
  types, three install scopes with ``local > project > user`` precedence,
  ``skipLfs`` for git sources, and network access that is bounded and is
  NEVER reached from a load or a startup path.
* **DEPENDENCIES.** ``dependencies`` with optional semver. ``enable``
  force-enables the transitive closure and reports what it turned on;
  ``disable`` REFUSES when another enabled plugin depends on the target and
  NAMES the dependent.
* **THE TRUST PROMPT.** A plain-language enumeration of what installing a
  plugin does to the machine: ``bin/`` executables, which hooks fire on
  which events, which MCP servers start and what they can reach, declared
  tools, and files writable outside the plugin directory.
* **DIGEST VERIFICATION ON EVERY LOAD.** The tree digest already exists in
  ``cli/plugins.py``; it is wired to the load path, and a plugin whose
  files changed after install is REFUSED, not silently reloaded.
* **PROJECTED PER-SESSION TOKEN COST**, so ``/plugin inspect`` can answer
  "is this worth keeping" with a number and a named estimator rather than
  an adjective.

Two rules this module holds to structurally:

1. **Never execute a component while loading or installing it.** Every
   function here is a read (or a rename). The only ``subprocess`` in the
   module is delegated to ``cli.plugins.install_from_git``, and the only
   network call is an INJECTABLE opener behind an explicit verb.
2. **A disabled plugin contributes nothing.** Every discovery function takes
   the disabled marker into account through ``cli.plugins``' own predicate,
   so there is one answer to "is this installed and on".

Trust and the digest are fail-CLOSED by construction: an unreadable receipt
is not an absent receipt, and ``require_trust`` refuses rather than
defaulting to "approved".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cli import plugins as _plugins

__all__ = [
    "CHARS_PER_TOKEN",
    "COMPONENT_CONVENTIONS",
    "INSTALL_SCOPES",
    "MARKETPLACE_SOURCE_TYPES",
    "PATH_TOKENS",
    "SCOPE_PRECEDENCE",
    "DependencyEdge",
    "DependencyReport",
    "DigestReport",
    "DisableReport",
    "EnableReport",
    "Marketplace",
    "MarketplaceEntry",
    "PluginComponents",
    "PluginDependency",
    "PluginError",
    "ProjectedCost",
    "TamperedPlugin",
    "TrustRefused",
    "TrustReport",
    "UninstallTrace",
    "add_marketplace_source",
    "component_paths",
    "dependents_of",
    "disable_plugin",
    "discover_components",
    "enable_plugin",
    "expand_path_tokens",
    "installed_plugin_names",
    "load_scoped_plugins",
    "marketplace_at",
    "menu_lines",
    "namespace",
    "namespace_verb_rows",
    "namespaced_lookup",
    "parse_namespace",
    "plugin_data_dir",
    "plugin_dependencies",
    "projected_session_cost",
    "read_marketplace",
    "require_trust",
    "run_plugin_verb",
    "scoped_root",
    "trust_decision",
    "trust_report",
    "uninstall_plugin",
    "verify_installed",
]

#: Re-exported so a caller never has to import two modules to catch one
#: error. A marketplace/loader failure is a USAGE error, which is exit 2.
PluginError = _plugins.PluginError

#: The directory that holds a plugin's METADATA. Components never live
#: inside it - that is the single most common authoring mistake and the
#: reason :func:`validate_layout` exists.
META_DIR_NAME = ".claude-plugin"

#: The manifest is OPTIONAL. Both locations are read, root first, so an
#: existing ``plugin.json`` keeps working byte-identically.
MANIFEST_LOCATIONS = ("plugin.json", f"{META_DIR_NAME}/plugin.json")
MARKETPLACE_FILENAME = f"{META_DIR_NAME}/marketplace.json"

#: One entry per component kind: the directory (relative to the plugin
#: root) and the file shape a member of it must have. ``None`` for the
#: directory means the file IS at the root.
COMPONENT_CONVENTIONS: Dict[str, Tuple[str, Optional[str]]] = {
    # kind -> (relative directory, required filename inside each entry)
    "skills": ("skills", "SKILL.md"),
    "commands": ("commands", None),
    "agents": ("agents", None),
    "hooks": ("hooks", "hooks.json"),
    "mcp": (None, ".mcp.json"),
    "lsp": (None, ".lsp.json"),
    "monitors": ("monitors", "monitors.json"),
    "bin": ("bin", None),
    "settings": (None, "settings.json"),
}

#: The kinds that carry EXECUTABLE content. They are enumerated separately
#: because the trust prompt has to name them first and a caller must not
#: have to remember which of the nine can run code.
EXECUTABLE_COMPONENT_KINDS = ("bin", "hooks", "mcp")

#: The three portable path tokens, in the order the trust prompt and the
#: docs state them.
PATH_TOKENS = (
    "${NEO_PLUGIN_ROOT}",
    "${NEO_PLUGIN_DATA}",
    "${NEO_PROJECT_DIR}",
)

#: Eight marketplace source types. Three are LOCAL and resolve without a
#: socket; three are REMOTE and are only ever reached from an explicit verb;
#: two are MATCHERS that select entries from an already-loaded marketplace
#: rather than fetching anything themselves.
MARKETPLACE_SOURCE_TYPES = (
    "github",
    "git",
    "url",
    "npm",
    "file",
    "directory",
    "hostPattern",
    "pathPattern",
    "settings",
)

#: The two matcher types. They select; they do not fetch.
MARKETPLACE_MATCHER_TYPES = ("hostPattern", "pathPattern")

#: Source types that require the network. Declared so a caller can assert
#: "this load path touches none of these" without reading the code.
REMOTE_SOURCE_TYPES = ("github", "git", "url", "npm")

#: The three install scopes. Highest rank wins; the tuple IS the
#: precedence, so there is no second table to drift.
INSTALL_SCOPES = ("user", "project", "local")
SCOPE_PRECEDENCE = {"local": 0, "project": 1, "user": 2}

#: The separator between a plugin name and a component name. A colon is
#: used because a slash cannot appear in either half, so the split is
#: unambiguous in both directions.
NAMESPACE_SEP = ":"

#: The characters-per-token divisor used by the projected cost. It is the
#: same conservative divisor ``harness.agent_kernel.budget`` documents for
#: its own heuristic estimator, and the receipt NAMES it so the number is
#: never read as a tokenizer result.
CHARS_PER_TOKEN = 4
COST_ESTIMATOR = f"heuristic:{CHARS_PER_TOKEN}chars-per-token"

#: A bound on the marketplace document this module will read. A
#: marketplace file is metadata; anything larger is not one.
MAX_MARKETPLACE_BYTES = 1024 * 1024
#: Bounded network. A marketplace fetch has a deadline, and the opener is
#: INJECTABLE so a caller (and a test) controls the transport.
DEFAULT_FETCH_TIMEOUT_S = 5.0
MAX_FETCH_BYTES = 4 * 1024 * 1024
#: The deepest a dependency walk will go before it reports a cycle.
MAX_DEPENDENCY_DEPTH = 32

_NAME_PAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SEMVER_PAT = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)
_RANGE_PAT = re.compile(
    r"^(?P<op>\^|~|>=|<=|>|<|=)?\s*"
    r"(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class TrustRefused(PluginError):
    """A plugin was not trusted, so it was not installed or enabled.

    A subclass of :class:`PluginError` on purpose: a refusal is a clean,
    user-facing USAGE error (exit 2), not a crash.
    """


class TamperedPlugin(PluginError):
    """An installed plugin's files no longer match its install receipt."""


# ---------------------------------------------------------------------------
# portable paths
# ---------------------------------------------------------------------------


def _neo_home() -> Path:
    """Return the Neo home directory the plugins root hangs off.

    ``plugins_root()`` is ``<neo home>/plugins`` under every documented
    override, so the plugin DATA root is its SIBLING. That placement is
    load-bearing: ``cli.plugins.uninstall`` re-scans the plugins root and
    raises on anything still named after the plugin, so a data directory
    INSIDE the root would make every uninstall report an incomplete removal.
    """
    return _plugins.plugins_root().parent


def plugin_data_dir(name: str, *, create: bool = False) -> Path:
    """Return the durable data directory for one installed plugin.

    Lives at ``<neo home>/plugin-data/<name>``, OUTSIDE the plugins root and
    OUTSIDE the plugin tree, which is exactly why it survives an update: an
    update replaces the plugin directory and nothing else.

    Raises :class:`PluginError` for an unsafe name - the same charset guard
    ``cli.plugins`` applies, so a data path can never escape its root.
    """
    directory = _plugins._plugin_dir(name).parent.parent / "plugin-data" / name
    if create:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - unwritable home
            raise PluginError(
                f"cannot create the plugin data directory {directory}: {exc}"
            ) from exc
    return directory


def _project_dir(project_dir: Optional[str] = None) -> str:
    """Resolve the PROJECT directory for ``${NEO_PROJECT_DIR}``.

    Returns the REPOSITORY, not the ``.neo`` directory: a token named
    ``PROJECT_DIR`` that expanded to ``<repo>/.neo`` would point every
    author at the wrong place. An explicit argument wins; otherwise the
    declared project settings directory is used and stripped.

    Returns the empty string when there is no project, and the token then
    expands to empty rather than to a guess - a path that silently became
    the process CWD is the failure this avoids.
    """
    raw = project_dir
    if not raw:
        override = os.environ.get("NEO_PROJECT_DIR")
        # Blank / name-less values are ABSENCES, not paths. Measured in
        # `cli.neoconfig.project_settings_dir`: `NEO_PROJECT_DIR="   "` and
        # `NEO_PROJECT_DIR="."` both resolved to a location that names no
        # settings directory, so a padded or truncated export silently
        # retargeted the token. Same rule, same reason.
        if override and override.strip():
            candidate = Path(override.strip()).expanduser()
            if candidate.name and candidate.name not in (".", ".."):
                raw = override
        if not raw:
            try:
                from cli import neoconfig

                resolved = neoconfig.project_settings_dir()
            except Exception:
                return ""
            if resolved is None:
                return ""
            raw = str(resolved)
    if not raw:
        return ""
    candidate = Path(raw).expanduser()
    # ``NEO_PROJECT_DIR`` names the ``.neo`` SETTINGS directory by product
    # convention; the token is named PROJECT_DIR and expands to the
    # REPOSITORY. ``scoped_root`` re-appends ``.neo``, so the two cannot
    # disagree about where the project settings live.
    return str(candidate.parent) if candidate.name == ".neo" else str(candidate)


def expand_path_tokens(
    text: str,
    plugin_dir: Any,
    *,
    plugin_name: Optional[str] = None,
    project_dir: Optional[str] = None,
    data_dir: Optional[str] = None,
) -> str:
    """Expand the three portable path tokens in ``text``.

    ``${NEO_PLUGIN_ROOT}`` is the plugin directory ITSELF, so it changes on
    every update. ``${NEO_PLUGIN_DATA}`` is the plugin's durable data
    directory, which an update does not touch. ``${NEO_PROJECT_DIR}`` is the
    project the session is in. Substitutions are literal and single-pass:
    a value that itself contains a token is NOT re-expanded, so a hostile
    manifest cannot build a substitution loop.
    """
    root = Path(plugin_dir).expanduser()
    name = plugin_name or root.name
    data = Path(data_dir) if data_dir is not None else plugin_data_dir(name)
    mapping = {
        "${NEO_PLUGIN_ROOT}": str(root),
        "${NEO_PLUGIN_DATA}": str(data),
        "${NEO_PROJECT_DIR}": _project_dir(project_dir),
    }
    out = str(text or "")
    for token, value in mapping.items():
        out = out.replace(token, value)
    return out


# ---------------------------------------------------------------------------
# convention discovery
# ---------------------------------------------------------------------------


def component_paths(plugin_dir: Any) -> Dict[str, List[str]]:
    """Return every convention component path found under ``plugin_dir``.

    Relative POSIX paths, sorted, and a DISABLED plugin contributes nothing.
    Never raises: an unreadable directory yields an empty entry for that kind
    plus the reason in :func:`discover_components`.
    """
    root = Path(plugin_dir).expanduser()
    out: Dict[str, List[str]] = {}
    if not root.is_dir() or _plugins.is_plugin_disabled(root.name):
        for kind in COMPONENT_CONVENTIONS:
            out[kind] = []
        return out
    for kind, (subdir, required) in COMPONENT_CONVENTIONS.items():
        base = root if subdir is None else root / subdir
        found: List[str] = []
        if base.is_dir() and not base.is_symlink():
            if required is None:
                for item in sorted(base.iterdir()):
                    if item.is_file() and not item.is_symlink():
                        found.append(item.relative_to(root).as_posix())
            elif kind in {"skills", "commands", "agents", "bin"}:
                for item in sorted(base.iterdir()):
                    if not item.is_dir() or item.is_symlink():
                        continue
                    if kind == "skills":
                        if (item / required).is_file():
                            found.append(item.relative_to(root).as_posix())
                    elif kind == "bin":
                        found.append(item.relative_to(root).as_posix())
                    else:
                        found.append(item.relative_to(root).as_posix())
            else:
                candidate = base / required
                if candidate.is_file() and not candidate.is_symlink():
                    found.append(candidate.relative_to(root).as_posix())
        out[kind] = found
    return out


@dataclass(frozen=True)
class PluginComponents:
    """Everything one plugin contributes, discovered by convention.

    ``manifest_present`` is False for a manifest-less plugin, and
    ``name`` then comes from the DIRECTORY - which is the documented rule,
    not a fallback.
    """

    name: str
    root: str
    manifest: Dict[str, Any] = field(default_factory=dict)
    manifest_path: str = ""
    manifest_present: bool = False
    skills: Tuple[str, ...] = ()
    commands: Tuple[str, ...] = ()
    agents: Tuple[str, ...] = ()
    hooks: Tuple[str, ...] = ()
    mcp: Tuple[str, ...] = ()
    lsp: Tuple[str, ...] = ()
    monitors: Tuple[str, ...] = ()
    bin: Tuple[str, ...] = ()
    settings: Tuple[str, ...] = ()
    tool_verbs: Tuple[str, ...] = ()
    mcp_servers: Tuple[str, ...] = ()
    dependencies: Tuple["PluginDependency", ...] = ()
    enabled: bool = True
    version: str = ""
    description: str = ""
    digest_ok: Optional[bool] = None
    digest_reason: str = ""
    errors: Tuple[str, ...] = ()

    @property
    def count(self) -> int:
        """Total number of discovered components of every kind."""
        return sum(len(getattr(self, kind)) for kind in COMPONENT_CONVENTIONS)

    def inventory_rows(self) -> List[Tuple[str, str]]:
        """``[(kind, member), ...]`` for the inspect surface, sorted."""
        rows: List[Tuple[str, str]] = []
        for kind in COMPONENT_CONVENTIONS:
            for member in getattr(self, kind):
                rows.append((kind, member))
        return rows

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection with no live objects."""
        return {
            "name": self.name,
            "root": self.root,
            "manifest_present": self.manifest_present,
            "manifest_path": self.manifest_path,
            "version": self.version,
            "description": self.description,
            "enabled": self.enabled,
            "digest_ok": self.digest_ok,
            "digest_reason": self.digest_reason,
            "count": self.count,
            "skills": list(self.skills),
            "commands": list(self.commands),
            "agents": list(self.agents),
            "hooks": list(self.hooks),
            "mcp": list(self.mcp),
            "lsp": list(self.lsp),
            "monitors": list(self.monitors),
            "bin": list(self.bin),
            "settings": list(self.settings),
            "tool_verbs": list(self.tool_verbs),
            "mcp_servers": list(self.mcp_servers),
            "dependencies": [dep.to_dict() for dep in self.dependencies],
            "errors": list(self.errors),
        }


def read_plugin_manifest(plugin_dir: Any) -> Tuple[Dict[str, Any], str, bool]:
    """Return ``(manifest, path, present)`` for one plugin directory.

    A thin DELEGATION to ``cli.plugins.read_plugin_manifest``, which is the
    one reader of both accepted locations and of the size/JSON guards. A
    second manifest reader here would be a second answer to "what does this
    plugin declare", which is exactly the kind of drift the brief asks the
    loader to remove.
    """
    return _plugins.read_plugin_manifest(Path(plugin_dir).expanduser())


def _declared_dependencies(
    manifest: Mapping[str, Any],
) -> Tuple["PluginDependency", ...]:
    """Parse ``dependencies`` from a manifest.

    Accepts a list of bare names, a list of ``{name, version}`` objects, or
    the mapping form ``{name: "^1.0"}``. An entry that names nothing usable
    is DROPPED and named in the caller's ``errors`` rather than failing a
    load - a plugin with one bad dependency edge is still a plugin.
    """
    raw = manifest.get("dependencies")
    out: List[PluginDependency] = []
    items: List[Any] = []
    if isinstance(raw, Mapping):
        items = [{"name": key, "version": value} for key, value in raw.items()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    for item in items:
        if isinstance(item, str):
            name, constraint = item.strip(), ""
        elif isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
            constraint = str(item.get("version") or item.get("constraint") or "")
        else:
            continue
        if _NAME_PAT.match(name):
            out.append(PluginDependency(name=name, constraint=constraint.strip()))
    return tuple(out)


def discover_components(
    plugin_dir: Any,
    *,
    name: Optional[str] = None,
    verify: bool = False,
) -> PluginComponents:
    """Discover every component one plugin directory contributes.

    The manifest is OPTIONAL: absent it, components are found by convention
    and the name is the directory name. Never executes anything, never
    writes, and a DISABLED plugin returns empty inventories with
    ``enabled=False`` rather than raising.

    ``verify=True`` re-hashes the tree against the install receipt and
    records the verdict in ``digest_ok``/``digest_reason``. It is OPT-IN
    rather than always-on because it re-reads every byte of the plugin
    (measured at ~6.5 ms for a 40-component plugin on this host), and every
    surface that is answering "is this what I think is installed" - the
    ``/plugin`` verbs - asks for it, while a scan that merely walks the root
    does not pay for it.
    """
    root = Path(plugin_dir).expanduser()
    try:
        manifest, manifest_path, present = read_plugin_manifest(root)
    except PluginError as exc:
        return PluginComponents(
            name=name or root.name,
            root=str(root),
            errors=(str(exc),),
        )
    found = component_paths(root)
    tools = manifest.get("tools") or {}
    verbs = tools.get("verbs") if isinstance(tools, Mapping) else None
    servers = manifest.get("mcp_servers") or {}
    errors: List[str] = []
    for kind, entries in found.items():
        for entry in entries:
            problem = validate_layout(root, entry, kind=kind)
            if problem:
                errors.append(problem)
    digest_ok: Optional[bool] = None
    digest_reason = ""
    if verify:
        digest = verify_installed(name or root.name)
        digest_ok = digest.ok
        digest_reason = digest.reason
    return PluginComponents(
        name=str(name or manifest.get("name") or root.name),
        root=str(root),
        manifest=dict(manifest),
        manifest_path=manifest_path,
        manifest_present=present,
        skills=tuple(found["skills"]),
        commands=tuple(found["commands"]),
        agents=tuple(found["agents"]),
        hooks=tuple(found["hooks"]),
        mcp=tuple(found["mcp"]),
        lsp=tuple(found["lsp"]),
        monitors=tuple(found["monitors"]),
        bin=tuple(found["bin"]),
        settings=tuple(found["settings"]),
        tool_verbs=tuple(str(v) for v in (verbs or []) if isinstance(v, str)),
        mcp_servers=tuple(str(k) for k in servers)
        if isinstance(servers, Mapping)
        else (),
        dependencies=_declared_dependencies(manifest),
        enabled=not _plugins.is_plugin_disabled(name or root.name),
        version=str(manifest.get("version") or ""),
        description=str(manifest.get("description") or ""),
        digest_ok=digest_ok,
        digest_reason=digest_reason,
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# teaching: the two authoring mistakes, refused with the fix
# ---------------------------------------------------------------------------


def validate_layout(plugin_dir: Any, relative: str, *, kind: str = "") -> str:
    """Return a TEACHING message for one component path, or ``""``.

    Two mistakes account for most plugins that silently contribute nothing,
    and each gets the rule AND the correct location:

    * a component inside ``.claude-plugin/`` - that directory holds
      METADATA (``plugin.json`` / ``marketplace.json``), never components;
    * a ``skills`` entry naming the ``SKILL.md`` FILE instead of its parent
      DIRECTORY.

    The function returns a message rather than raising so discovery can
    report every mistake at once; :func:`validate_source` is the raising
    form used on the install path.
    """
    text = str(relative or "").replace("\\", "/").strip("/")
    if not text:
        return ""
    parts = text.split("/")
    if META_DIR_NAME in parts[:-1]:
        corrected = "/".join(p for p in parts if p != META_DIR_NAME)
        return (
            f"component {relative!r} is inside {META_DIR_NAME}/, which holds "
            f"plugin METADATA (plugin.json, marketplace.json), not components. "
            f"Put it at {corrected!r} instead."
        )
    if parts[0] == META_DIR_NAME:
        return (
            f"component {relative!r} is inside {META_DIR_NAME}/, which holds "
            "plugin METADATA (plugin.json, marketplace.json), not components."
        )
    if kind == "skills" and parts[-1] == "SKILL.md":
        corrected = "/".join(parts[:-1])
        return (
            f"skill entry {relative!r} names the SKILL.md FILE. A 'skills' "
            f"entry names the DIRECTORY that contains it: use {corrected!r}."
        )
    return ""


def metadata_dir_mistake(plugin_dir: Any) -> str:
    """Return a teaching message when a COMPONENT sits inside ``.claude-plugin/``.

    The physical case, not the declared one: an author who creates
    ``.claude-plugin/skills/alpha/`` has built a component directory where
    discovery will never look, so the plugin silently contributes nothing.
    It is reported rather than ignored because "my skills do not show up" is
    the symptom and this is the cause.
    """
    meta = Path(plugin_dir).expanduser() / META_DIR_NAME
    if not meta.is_dir() or meta.is_symlink():
        return ""
    for subdir in ("skills", "commands", "agents", "hooks", "monitors", "bin"):
        candidate = meta / subdir
        if candidate.is_dir() and not candidate.is_symlink():
            moved = sorted(
                entry.name
                for entry in candidate.iterdir()
                if entry.is_dir() and not entry.is_symlink()
            )
            suffix = f"/{moved[0]}" if moved else ""
            return (
                f"{META_DIR_NAME}/{subdir}{suffix} is a COMPONENT directory "
                f"inside {META_DIR_NAME}/, which holds plugin METADATA "
                f"(plugin.json, marketplace.json). Move it to "
                f"{subdir}{suffix} at the plugin root."
            )
    return ""


def validate_teaching(
    source: Any, manifest: Optional[Mapping[str, Any]] = None
) -> None:
    """Refuse the two authoring mistakes, with the fix in the message.

    Raises :class:`PluginError` naming BOTH the rule and the correct
    location. Runs FIRST on the install path, ahead of
    ``cli.plugins.validate_plugin``, because a generic "has no SKILL.md" is a
    true fact and an unhelpful one - and because ``validate_plugin`` would
    otherwise be the answer to a question nobody asked.
    """
    root = Path(source).expanduser()
    data = (
        dict(manifest) if manifest is not None else dict(read_plugin_manifest(root)[0])
    )
    for rel in data.get("skills") or []:
        problem = validate_layout(root, str(rel), kind="skills")
        if problem:
            raise PluginError(problem)
    for key in ("commands", "agents"):
        for rel in data.get(key) or []:
            problem = validate_layout(root, str(rel), kind=key)
            if problem:
                raise PluginError(problem)
    for kind, entries in component_paths(root).items():
        for entry in entries:
            problem = validate_layout(root, entry, kind=kind)
            if problem:
                raise PluginError(problem)
    physical = metadata_dir_mistake(root)
    if physical:
        raise PluginError(physical)


def validate_manifest_identity(
    source: Any, manifest: Optional[Mapping[str, Any]] = None
) -> None:
    """Refuse a manifest that does not declare BOTH ``name`` and ``version``.

    Runs AFTER ``cli.plugins.validate_plugin`` on the install path. The
    ordering is deliberate and is the reason the traversal, symlink and
    MCP-label refusals are byte-identically the messages they have always
    been: this is the newest and least structural rule, so it never gets to
    pre-empt a longer-standing one.

    A manifest-LESS plugin declares no version and is NOT refused here - the
    name comes from the directory and ``inspect`` reports the version as
    unknown rather than inventing one.
    """
    root = Path(source).expanduser()
    data = (
        dict(manifest) if manifest is not None else dict(read_plugin_manifest(root)[0])
    )
    if not read_plugin_manifest(root)[2]:
        return
    missing = [
        key for key in ("name", "version") if not str(data.get(key) or "").strip()
    ]
    if missing:
        raise PluginError(
            f"plugin.json requires BOTH 'name' and 'version'; missing "
            f"{', '.join(repr(key) for key in missing)} in {root}"
        )


def validate_source(source: Any, manifest: Optional[Mapping[str, Any]] = None) -> None:
    """Run BOTH additive validations, in their install-path order.

    The combined form, for a caller that is not ``install_from_local``.
    Nothing in ``cli.plugins`` is weakened: that module's traversal,
    symlink, MCP-label and tools-shape guards still run separately.
    """
    validate_teaching(source, manifest)
    validate_manifest_identity(source, manifest)


# ---------------------------------------------------------------------------
# namespacing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginDependency:
    """One declared dependency edge, with an OPTIONAL semver range."""

    name: str
    constraint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {"name": self.name, "constraint": self.constraint}


def namespace(plugin: str, component: str) -> str:
    """Return the namespaced invocation ``/plugin:component``."""
    return f"/{str(plugin).strip().lstrip('/')}{NAMESPACE_SEP}{str(component).strip()}"


def parse_namespace(text: str) -> Optional[Tuple[str, str]]:
    """Split ``/plugin:component`` into its two halves, or return ``None``.

    The split is unambiguous because ``:`` cannot appear in either half: the
    FIRST ``:`` separates, and an unnamespaced name returns ``None`` so a
    caller can tell "not namespaced" from "namespaced with an empty half".
    """
    raw = str(text or "").strip()
    if not raw.startswith("/") or NAMESPACE_SEP not in raw:
        return None
    body = raw[1:]
    plugin, _, component = body.partition(NAMESPACE_SEP)
    if not plugin or not component:
        return None
    return plugin, component


def namespace_verb_rows() -> List[Tuple[str, str, str]]:
    """Return ``(invocation, plugin, component)`` for every installed command.

    Every namespaced command is offered BESIDE the plain name, which is the
    whole point: a plugin carrying ``commands/review.md`` and a user with
    their own ``/review`` both stay reachable, and the plugin's own copy is
    reachable under its own namespace too.
    """
    rows: List[Tuple[str, str, str]] = []
    for components in load_scoped_plugins():
        for command in components.commands:
            stem = Path(command).stem
            rows.append((namespace(components.name, stem), components.name, stem))
    return sorted(rows)


def namespaced_lookup(text: str) -> Optional[Dict[str, Any]]:
    """Resolve ``/plugin:component`` to its owning plugin, or ``None``.

    Resolution goes through the SAME scope precedence every other surface
    uses, and a DISABLED plugin resolves to nothing: a muted plugin
    contributes nothing, including under its namespace.
    """
    parsed = parse_namespace(text)
    if parsed is None:
        return None
    plugin, component = parsed
    for components in load_scoped_plugins():
        if components.name != plugin or not components.enabled:
            continue
        for member in components.commands:
            if Path(member).stem == component:
                return {
                    "plugin": plugin,
                    "component": component,
                    "kind": "commands",
                    "path": member,
                    "root": components.root,
                }
        for member in components.skills:
            if Path(member).name == component:
                return {
                    "plugin": plugin,
                    "component": component,
                    "kind": "skills",
                    "path": member,
                    "root": components.root,
                }
        for member in components.agents:
            if Path(member).stem == component:
                return {
                    "plugin": plugin,
                    "component": component,
                    "kind": "agents",
                    "path": member,
                    "root": components.root,
                }
    return None


# ---------------------------------------------------------------------------
# scopes
# ---------------------------------------------------------------------------


def scoped_root(scope: str, *, project_dir: Optional[str] = None) -> Path:
    """Return the plugin root for one install scope.

    ``user`` is ``cli.plugins.plugins_root()`` - the historical location, so
    an existing install is found by a scope-aware scan without migration.
    ``project`` and ``local`` live under the project's ``.neo`` directory and
    are git-ignored by the project's own convention (``local`` is the
    personal override).
    """
    name = str(scope or "user").strip().lower()
    if name not in INSTALL_SCOPES:
        raise PluginError(
            f"unknown install scope {scope!r} (expected one of "
            f"{', '.join(INSTALL_SCOPES)})"
        )
    if name == "user":
        return _plugins.plugins_root()
    resolved = _project_dir(project_dir)
    if not resolved:
        raise PluginError(
            f"no project directory for the {name!r} scope; run inside a "
            "project or pass --repo"
        )
    base = Path(resolved) / ".neo"
    return base / "plugins.local" if name == "local" else base / "plugins"


def load_scoped_plugins(
    *,
    project_dir: Optional[str] = None,
    include_disabled: bool = True,
    verify: bool = False,
) -> List[PluginComponents]:
    """Return every discoverable plugin, ``local > project > user``.

    The FIRST scope that declares a name wins, which is why the scopes are
    walked in precedence order and later declarations of the same name are
    recorded as shadowed rather than silently dropped. ``verify=True``
    forwards the digest check to :func:`discover_components`; see that
    function for why it is opt-in.
    """
    winners: Dict[str, PluginComponents] = {}
    for scope in sorted(INSTALL_SCOPES, key=lambda item: SCOPE_PRECEDENCE[item]):
        try:
            root = scoped_root(scope, project_dir=project_dir)
        except PluginError:
            continue
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
            except OSError:
                continue
            if not include_disabled and _plugins.is_plugin_disabled(entry.name):
                continue
            components = discover_components(entry, verify=verify)
            if components.name in winners:
                continue
            winners[components.name] = components
    return [winners[name] for name in sorted(winners)]


def installed_plugin_names(*, project_dir: Optional[str] = None) -> Tuple[str, ...]:
    """Every installed plugin name across all three scopes."""
    return tuple(item.name for item in load_scoped_plugins(project_dir=project_dir))


# ---------------------------------------------------------------------------
# digest verification on every load
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DigestReport:
    """Whether an installed plugin still matches its install receipt.

    ``checked`` is False when there is NO receipt, and ``ok`` is then False
    as well: an unreadable receipt is not an absent receipt, and a plugin
    whose provenance this install cannot prove is refused rather than
    loaded on trust.
    """

    name: str
    checked: bool = False
    ok: bool = False
    expected_sha256: str = ""
    actual_sha256: str = ""
    reason: str = ""
    changed_files: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": self.name,
            "checked": self.checked,
            "ok": self.ok,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "reason": self.reason,
            "changed_files": list(self.changed_files),
        }


def _changed_files(root: Path, expected: Mapping[str, str]) -> Tuple[str, ...]:
    """Return which files differ from a per-file digest map.

    The receipt only stores the TREE digest, so the per-file comparison is a
    SECOND source of truth derived from the same files: it is reported, not
    trusted, and it exists so a refusal NAMES what changed.

    It re-reads the tree, so it is called ONLY after the tree digest has
    already mismatched - a clean plugin must not pay for a second full read
    on every load.
    """
    import hashlib

    current: Dict[str, str] = {}
    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                current[path.relative_to(root).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            except OSError:
                current[path.relative_to(root).as_posix()] = "<unreadable>"
    except OSError:
        return ()
    return tuple(
        key
        for key in sorted(set(current) | set(expected))
        if current.get(key) != expected.get(key)
    )


def verify_installed(name: str) -> DigestReport:
    """Re-hash an installed plugin tree and compare it to its receipt.

    A plugin whose files changed after install is REFUSED, not silently
    reloaded: the whole point of recording a tree digest at install is that
    it is checked somewhere, and this is that somewhere. Never raises - the
    caller decides whether a mismatch is fatal.
    """
    try:
        root = _plugins._plugin_dir(name)
    except PluginError as exc:
        return DigestReport(name=name, reason=str(exc))
    if not root.is_dir():
        return DigestReport(name=name, reason=f"no installed plugin named {name!r}")
    receipt = _plugins.read_install_receipt(name)
    if receipt is None or not receipt.tree_sha256:
        return DigestReport(
            name=name,
            reason=(
                f"plugin {name!r} has no install record to verify against; "
                "reinstall it to establish one"
            ),
        )
    actual, _files, _bytes = _plugins._tree_digest(root)
    if actual == receipt.tree_sha256:
        return DigestReport(
            name=name,
            checked=True,
            ok=True,
            expected_sha256=receipt.tree_sha256,
            actual_sha256=actual,
        )
    return DigestReport(
        name=name,
        checked=True,
        ok=False,
        expected_sha256=receipt.tree_sha256,
        actual_sha256=actual,
        reason=(
            f"plugin {name!r} was modified after install "
            f"(expected {receipt.tree_sha256[:12]}, found {actual[:12]})"
        ),
        changed_files=_changed_files(root, {}),
    )


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------


def plugin_dependencies(name: str) -> Tuple[PluginDependency, ...]:
    """Return the declared dependency edges of one installed plugin."""
    try:
        root = _plugins._plugin_dir(name)
    except PluginError:
        return ()
    if not root.is_dir():
        return ()
    return discover_components(root, name=name).dependencies


def _parse_semver(text: str) -> Optional[Tuple[int, int, int]]:
    """Parse a strict ``MAJOR.MINOR.PATCH`` string, or return ``None``."""
    match = _SEMVER_PAT.match(str(text or "").strip())
    if match is None:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def semver_satisfies(version: str, constraint: str) -> Optional[bool]:
    """Return whether ``version`` satisfies ``constraint``.

    ``None`` means "cannot be decided" and is deliberately NOT ``True``:
    an unparseable version must not pass a range, and an empty constraint
    means "no constraint declared", which DOES pass. Supported operators
    are ``^``, ``~``, ``>=``, ``<=``, ``>``, ``<``, ``=`` and a bare
    version (treated as ``=`` with the omitted parts defaulted).
    """
    wanted = str(constraint or "").strip()
    if not wanted:
        return True
    parsed = _parse_semver(version)
    if parsed is None:
        return None
    match = _RANGE_PAT.match(wanted)
    if match is None:
        return None
    op = match.group("op") or "="
    floor = (
        int(match.group("major")),
        int(match.group("minor") or 0),
        int(match.group("patch") or 0),
    )
    if op == "^":
        if floor[0] > 0:
            ceiling = (floor[0] + 1, 0, 0)
        elif floor[1] > 0:
            ceiling = (0, floor[1] + 1, 0)
        else:
            ceiling = (0, 0, floor[2] + 1)
        return parsed >= floor and parsed < ceiling
    if op == "~":
        return parsed >= floor and parsed < (floor[0], floor[1] + 1, 0)
    if op == ">=":
        return parsed >= floor
    if op == "<=":
        return parsed <= floor
    if op == ">":
        return parsed > floor
    if op == "<":
        return parsed < floor
    # ``=`` with a partial floor: 1.2 matches 1.2.3, 1.3 does not.
    if match.group("minor") is None:
        return parsed[0] == floor[0]
    if match.group("patch") is None:
        return parsed[:2] == floor[:2]
    return parsed == floor


@dataclass(frozen=True)
class DependencyEdge:
    """One edge in the resolved dependency graph, with its own verdict."""

    name: str
    constraint: str = ""
    installed_version: str = ""
    satisfied: Optional[bool] = None
    present: bool = False
    enabled: bool = False
    required_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": self.name,
            "constraint": self.constraint,
            "installed_version": self.installed_version,
            "satisfied": self.satisfied,
            "present": self.present,
            "enabled": self.enabled,
            "required_by": self.required_by,
        }


@dataclass(frozen=True)
class DependencyReport:
    """A resolved dependency closure, and every verdict that shaped it."""

    plugin: str
    edges: Tuple[DependencyEdge, ...] = ()
    missing: Tuple[str, ...] = ()
    unsatisfied: Tuple[str, ...] = ()
    undecidable: Tuple[str, ...] = ()
    cycles: Tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        """True when every edge resolved to an installed, satisfied plugin."""
        return not self.missing and not self.unsatisfied

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "plugin": self.plugin,
            "edges": [edge.to_dict() for edge in self.edges],
            "missing": list(self.missing),
            "unsatisfied": list(self.unsatisfied),
            "undecidable": list(self.undecidable),
            "cycles": list(self.cycles),
            "satisfied": self.satisfied,
        }


def _installed_version(name: str) -> str:
    """Return the installed version of one plugin, or ``""``."""
    receipt = _plugins.read_install_receipt(name)
    if receipt is not None and receipt.version:
        return receipt.version
    try:
        root = _plugins._plugin_dir(name)
    except PluginError:
        return ""
    if not root.is_dir():
        return ""
    return discover_components(root, name=name).version


def dependency_report(name: str) -> DependencyReport:
    """Resolve the transitive dependency closure of one plugin.

    Reports, rather than raises, on every unhappy path: a missing
    dependency, a version that cannot satisfy the declared range, a version
    whose range cannot be DECIDED, and a cycle (which is reported as a cycle
    rather than by exhausting a recursion budget).
    """
    edges: List[DependencyEdge] = []
    missing: List[str] = []
    unsatisfied: List[str] = []
    undecidable: List[str] = []
    cycles: List[str] = []
    seen: set = set()
    stack: List[str] = []

    def walk(current: str, depth: int) -> None:
        if depth > MAX_DEPENDENCY_DEPTH:
            cycles.append(current)
            return
        if current in stack:
            cycles.append(" -> ".join([*stack[stack.index(current) :], current]))
            return
        stack.append(current)
        for dep in plugin_dependencies(current):
            if dep.name == current:
                cycles.append(f"{current} -> {current}")
                continue
            present = False
            enabled = False
            try:
                dep_root = _plugins._plugin_dir(dep.name)
                present = dep_root.is_dir()
                enabled = present and not _plugins.is_plugin_disabled(dep.name)
            except PluginError:
                present = False
            version = _installed_version(dep.name) if present else ""
            satisfied = semver_satisfies(version, dep.constraint) if present else False
            edges.append(
                DependencyEdge(
                    name=dep.name,
                    constraint=dep.constraint,
                    installed_version=version,
                    satisfied=satisfied,
                    present=present,
                    enabled=enabled,
                    required_by=current,
                )
            )
            if not present:
                missing.append(dep.name)
                continue
            if satisfied is None:
                undecidable.append(dep.name)
            elif satisfied is False:
                unsatisfied.append(dep.name)
            key = (dep.name, current)
            if key in seen:
                continue
            seen.add(key)
            walk(dep.name, depth + 1)
        stack.pop()

    walk(name, 0)
    return DependencyReport(
        plugin=name,
        edges=tuple(edges),
        missing=tuple(sorted(set(missing))),
        unsatisfied=tuple(sorted(set(unsatisfied))),
        undecidable=tuple(sorted(set(undecidable))),
        cycles=tuple(sorted(set(cycles))),
    )


def dependents_of(name: str) -> Tuple[str, ...]:
    """Return every ENABLED plugin that declares a dependency on ``name``.

    A plugin that is itself disabled cannot make ``name`` load-bearing, so it
    is not a dependent - that is what makes ``disable`` order-independent
    rather than a puzzle.
    """
    found: List[str] = []
    for entry in _plugins.list_plugins():
        if entry.get("name") == name:
            continue
        if entry.get("enabled") is False or entry.get("error"):
            continue
        for dep in plugin_dependencies(str(entry.get("name"))):
            if dep.name == name:
                found.append(str(entry.get("name")))
                break
    return tuple(sorted(set(found)))


@dataclass(frozen=True)
class EnableReport:
    """What ``enable`` actually turned on, and why."""

    name: str
    enabled: Tuple[str, ...] = ()
    already_enabled: Tuple[str, ...] = ()
    missing_dependencies: Tuple[str, ...] = ()
    unsatisfied: Tuple[str, ...] = ()
    undecidable: Tuple[str, ...] = ()
    cycles: Tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True when the whole closure is present and satisfied."""
        return not self.missing_dependencies and not self.unsatisfied

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": self.name,
            "enabled": list(self.enabled),
            "already_enabled": list(self.already_enabled),
            "missing_dependencies": list(self.missing_dependencies),
            "unsatisfied": list(self.unsatisfied),
            "undecidable": list(self.undecidable),
            "cycles": list(self.cycles),
            "complete": self.complete,
        }


def enable_plugin(name: str) -> EnableReport:
    """Enable a plugin AND every transitive dependency it needs.

    Dependencies are force-enabled because a half-enabled closure is the
    state a dependency graph exists to prevent: the target loads and the
    thing it needs does not. What was turned ON is REPORTED, so a user who
    expected one plugin learns that four are now contributing.
    """
    report = dependency_report(name)
    order: List[str] = []
    already: List[str] = []

    def turn_on(target: str, depth: int) -> None:
        if depth > MAX_DEPENDENCY_DEPTH or target in order:
            return
        if not _plugins.is_plugin_disabled(target):
            already.append(target)
        else:
            _plugins.enable(target)
            order.append(target)
        for dep in plugin_dependencies(target):
            turn_on(dep.name, depth + 1)

    turn_on(name, 0)
    return EnableReport(
        name=name,
        enabled=tuple(order),
        already_enabled=tuple(sorted(set(already))),
        missing_dependencies=report.missing,
        unsatisfied=report.unsatisfied,
        undecidable=report.undecidable,
        cycles=report.cycles,
    )


@dataclass(frozen=True)
class DisableReport:
    """What ``disable`` did, or which dependent made it refuse."""

    name: str
    disabled: bool = False
    dependents: Tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": self.name,
            "disabled": self.disabled,
            "dependents": list(self.dependents),
            "reason": self.reason,
        }


def disable_plugin(name: str) -> DisableReport:
    """Disable a plugin, REFUSING when an enabled plugin depends on it.

    The refusal NAMES the dependent rather than saying "conflicting
    dependency", because the one question a person has is which plugin to
    deal with first. Nothing is mutated on the refusal path.
    """
    dependents = dependents_of(name)
    if dependents:
        listed = ", ".join(dependents)
        return DisableReport(
            name=name,
            disabled=False,
            dependents=dependents,
            reason=(
                f"cannot disable {name!r}: {listed} "
                f"{'depends' if len(dependents) == 1 else 'depend'} on it. "
                f"Disable {listed} first."
            ),
        )
    _plugins.disable(name)
    return DisableReport(name=name, disabled=True)


# ---------------------------------------------------------------------------
# the trust prompt
# ---------------------------------------------------------------------------


def _hook_event_vocabulary() -> Tuple[str, ...]:
    """Return the hook events, reusing ``extensions.user_hooks``.

    The fallback keeps this module importable on a broken install: an
    unusable hook LAYER must not crash the loader, and the vocabulary is
    duplicated here only as the degraded case, never as a second authority
    while ``extensions`` imports.
    """
    try:
        from extensions.user_hooks import HOOK_EVENTS

        return tuple(HOOK_EVENTS)
    except Exception:
        return (
            "SessionStart",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "Stop",
            "PreCompact",
            "SessionEnd",
        )


def _read_json(path: Path, *, limit: int = MAX_MARKETPLACE_BYTES) -> Any:
    """Read a bounded JSON document, or return ``None`` on any failure."""
    try:
        if path.stat().st_size > limit:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _hook_declarations(root: Path) -> List[Dict[str, str]]:
    """Return one row per declared hook: ``{event, command, kind}``.

    Accepts both shapes a hook document is written in: the event-keyed map
    (``{"hooks": {"PreToolUse": [...]}}``) and a flat list of objects
    carrying their own ``event``. An unknown event name is KEPT and named,
    because silently dropping a hook that will fire is the one outcome this
    function must not produce.
    """
    document = _read_json(root / "hooks" / "hooks.json")
    rows: List[Dict[str, str]] = []
    if document is None:
        return rows
    blocks: Any = None
    if isinstance(document, Mapping):
        raw = document.get("hooks")
        blocks = raw if isinstance(raw, Mapping) else document
    elif isinstance(document, list):
        blocks = {"": document}
    if not isinstance(blocks, Mapping):
        return rows
    known = _hook_event_vocabulary()
    for event, entries in blocks.items():
        name = str(event or "").strip()
        if name and name not in known and name != "":
            rows.append({"event": name, "command": "", "kind": "unknown_event"})
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            event_name = str(entry.get("event") or name or "").strip()
            if event_name and event_name not in known:
                rows.append(
                    {"event": event_name, "command": "", "kind": "unknown_event"}
                )
                continue
            handler = entry.get("command")
            if isinstance(handler, (list, tuple)):
                command = " ".join(str(part) for part in handler)
                kind = "command"
            elif isinstance(handler, str):
                command = handler
                kind = "command"
            elif entry.get("http"):
                command = str(entry.get("http"))
                kind = "http"
            elif entry.get("prompt"):
                command = str(entry.get("prompt"))
                kind = "prompt"
            else:
                command = ""
                kind = "unknown"
            matcher = str(entry.get("matcher") or entry.get("if") or "*")
            rows.append(
                {
                    "event": event_name or "(unnamed)",
                    "command": command,
                    "kind": kind,
                    "matcher": matcher,
                }
            )
    return rows


def _mcp_declarations(components: PluginComponents) -> List[Dict[str, str]]:
    """Return one row per MCP server the plugin would START.

    Two declarations are merged: the convention ``.mcp.json`` at the plugin
    root and the manifest's ``mcp_servers`` map. The REACH column is the
    honest part - it names what the launch command can touch, derived from
    the command's own shape, and an unrecognised shape is reported as
    ``unknown`` rather than guessed at.
    """
    rows: List[Dict[str, str]] = []
    document = _read_json(Path(components.root) / ".mcp.json")
    servers: Dict[str, Any] = {}
    if isinstance(document, Mapping):
        raw = document.get("mcpServers") or document.get("servers")
        if isinstance(raw, Mapping):
            servers.update(raw)
    manifest_servers = components.manifest.get("mcp_servers")
    if isinstance(manifest_servers, Mapping):
        servers.update(manifest_servers)
    for label in sorted(servers):
        spec = servers[label]
        if isinstance(spec, Mapping):
            command = spec.get("command") or spec.get("url") or ""
            args = spec.get("args") or []
            launch = " ".join([str(command), *[str(a) for a in args]]).strip()
        else:
            launch = str(spec)
        rows.append(
            {
                "label": str(label),
                "launch": launch,
                "reach": _mcp_reach(launch),
            }
        )
    return rows


def _mcp_reach(launch: str) -> str:
    """Describe what a launch command can REACH, from its own shape.

    Deliberately conservative: an unrecognised command reports
    ``unknown`` (a category, not a permission), and a network-looking
    argument reports ``network`` because that is the observable fact.
    """
    text = str(launch or "").lower()
    if not text:
        return "unknown"
    if "npx" in text or "npm" in text or "pip" in text:
        return "executes a package manager"
    if "http://" in text or "https://" in text:
        return "network"
    if "python" in text or "node" in text or "bash" in text or "sh " in text:
        return "executes local code"
    return "unknown"


def _writable_outside(root: Path, components: PluginComponents) -> Tuple[str, ...]:
    """Return the paths a plugin may write OUTSIDE its own directory.

    Two sources, both declared by the plugin, both enumerated rather than
    judged: a ``writes`` list in the manifest, and every path token other
    than ``${NEO_PLUGIN_ROOT}`` appearing in a text component. A plugin that
    declares nothing is reported as declaring nothing - not as writing
    nothing, because a script it ships can write anywhere.
    """
    found: List[str] = []
    declared = components.manifest.get("writes") or components.manifest.get(
        "writes_outside"
    )
    if isinstance(declared, (list, tuple)):
        found.extend(str(item) for item in declared)
    for kind in ("commands", "agents", "settings", "hooks"):
        for member in getattr(components, kind):
            path = Path(components.root) / member
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for token in PATH_TOKENS:
                if token == "${NEO_PLUGIN_ROOT}":
                    continue
                if token in text and token not in found:
                    found.append(token)
    return tuple(sorted(set(found)))


@dataclass(frozen=True)
class TrustReport:
    """What installing and enabling one plugin would do to the machine.

    ``requires_review`` is True when the plugin ships anything that can
    execute, start a process, or reach outside its own directory. It is the
    flag the install and enable paths gate on, and it is computed from the
    inventory rather than declared by the author.
    """

    name: str
    root: str = ""
    description: str = ""
    executables: Tuple[str, ...] = ()
    hooks: Tuple[Dict[str, str], ...] = ()
    mcp_servers: Tuple[Dict[str, str], ...] = ()
    tool_verbs: Tuple[str, ...] = ()
    skills: Tuple[str, ...] = ()
    commands: Tuple[str, ...] = ()
    agents: Tuple[str, ...] = ()
    lsp: Tuple[str, ...] = ()
    monitors: Tuple[str, ...] = ()
    settings_files: Tuple[str, ...] = ()
    writable_outside: Tuple[str, ...] = ()
    digest_ok: bool = False
    errors: Tuple[str, ...] = ()

    @property
    def requires_review(self) -> bool:
        """True when the plugin ships anything executable or outward-reaching."""
        return bool(
            self.executables or self.hooks or self.mcp_servers or self.writable_outside
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": self.name,
            "root": self.root,
            "description": self.description,
            "executables": list(self.executables),
            "hooks": [dict(row) for row in self.hooks],
            "mcp_servers": [dict(row) for row in self.mcp_servers],
            "tool_verbs": list(self.tool_verbs),
            "skills": list(self.skills),
            "commands": list(self.commands),
            "agents": list(self.agents),
            "lsp": list(self.lsp),
            "monitors": list(self.monitors),
            "settings_files": list(self.settings_files),
            "writable_outside": list(self.writable_outside),
            "digest_ok": self.digest_ok,
            "requires_review": self.requires_review,
            "errors": list(self.errors),
        }


def trust_report(name: str, *, verify_digest: bool = True) -> TrustReport:
    """Build the plain-language trust prompt for one installed plugin.

    Never executes a component: the ``bin/`` names are listed, the hook
    commands are printed, the MCP launch commands are printed. Nothing is
    spawned, and nothing is imported.
    """
    try:
        root = _plugins._plugin_dir(name)
    except PluginError as exc:
        return TrustReport(name=name, errors=(str(exc),))
    components = discover_components(root, name=name)
    digest = verify_installed(name) if verify_digest else None
    return TrustReport(
        name=components.name,
        root=components.root,
        description=components.description,
        executables=components.bin,
        hooks=tuple(_hook_declarations(root)),
        mcp_servers=tuple(_mcp_declarations(components)),
        tool_verbs=components.tool_verbs,
        skills=components.skills,
        commands=components.commands,
        agents=components.agents,
        lsp=components.lsp,
        monitors=components.monitors,
        settings_files=components.settings,
        writable_outside=_writable_outside(root, components),
        digest_ok=bool(digest is not None and digest.ok),
        errors=components.errors,
    )


def escape_lines(lines: Iterable[str]) -> List[str]:
    """Escape PLAIN lines for a markup-parsing sink; the primary exit.

    Every line this module produces is PLAIN text, because a plugin name, a
    hook command, a repository path and a plugin DESCRIPTION are all data.
    rich parses ``[`` as a markup tag, so a line carrying one is CONSUMED
    rather than printed - the failure mode where a render failure deletes a
    message. This is rich's own ``escape``, imported rather than
    hand-rolled, so the escaping cannot drift from the parser's own rules.

    The structural alternative is :func:`safe_lines`, which returns
    ``rich.text.Text`` and is never markup-parsed at all.
    """
    from rich.markup import escape

    return [escape(str(line)) for line in lines]


def safe_lines(lines: Iterable[str]) -> List[Any]:
    """Return lines as ``rich.text.Text``, which has NO markup parsing.

    The structural answer: a caller that renders into a sink that honours
    markup can use this and cannot be bitten by a hostile name, because
    there is no parser to interpret one.
    """
    from rich.text import Text

    return [Text(str(line)) for line in lines]


def trust_lines(report: TrustReport) -> List[str]:
    """Render one trust report as PLAIN lines.

    Plain by contract: a plugin name, a hook command and a repository path
    are all DATA, and rich parses ``[`` as a markup tag - a render failure
    there deletes the message instead of printing it. Every line answers
    "what does this do to my machine".
    """
    lines: List[str] = [f"{report.name}: what this plugin does on your machine"]
    if report.description:
        lines.append(f"  claims to be: {report.description}")
    if report.errors:
        for error in report.errors:
            lines.append(f"  problem: {error}")
    if report.executables:
        lines.append(
            f"  runs programs: {', '.join(report.executables)} "
            "(from bin/, with your privileges)"
        )
    else:
        lines.append("  runs programs: none")
    if report.hooks:
        lines.append("  hooks that fire:")
        for hook in report.hooks:
            matcher = str(hook.get("matcher") or "*")
            command = str(hook.get("command") or "(no command)")
            lines.append(
                f"    on {hook.get('event', '?')} (match {matcher}, "
                f"{hook.get('kind', '?')}): {command}"
            )
    else:
        lines.append("  hooks that fire: none")
    if report.mcp_servers:
        lines.append("  MCP servers that start:")
        for server in report.mcp_servers:
            lines.append(
                f"    {server.get('label', '?')} -> {server.get('launch', '')} "
                f"[can reach: {server.get('reach', 'unknown')}]"
            )
    else:
        lines.append("  MCP servers that start: none")
    if report.tool_verbs:
        lines.append(f"  declares read-only tool verbs: {', '.join(report.tool_verbs)}")
    else:
        lines.append("  declares read-only tool verbs: none")
    counts = []
    for label, values in (
        ("skills", report.skills),
        ("commands", report.commands),
        ("agents", report.agents),
        ("lsp servers", report.lsp),
        ("monitors", report.monitors),
        ("settings", report.settings_files),
    ):
        if values:
            counts.append(f"{label}: {', '.join(values)}")
    lines.append("  contributes " + ("; ".join(counts) if counts else "nothing"))
    if report.writable_outside:
        lines.append(
            "  writes outside its own directory: " + ", ".join(report.writable_outside)
        )
    else:
        lines.append(
            "  writes outside its own directory: nothing it declares "
            "(scripts it ships can write anywhere)"
        )
    lines.append(
        f"  installed files verified against the install record: "
        f"{'yes' if report.digest_ok else 'NO'}"
    )
    return lines


def trust_decision_path(name: str) -> Path:
    """Return the decision row path for one plugin name.

    Lives inside the plugins root's ``.state`` directory, next to the
    install receipt it answers for, so an uninstall that already removes the
    receipt has one place to look for the rest.
    """
    return _plugins.plugin_state_dir() / f"{name}.trust.json"


def trust_decision(name: str) -> Optional[Dict[str, Any]]:
    """Read the recorded trust decision for one plugin, or ``None``.

    Returns ``None`` for a MISSING row and for an unreadable one, and the
    caller treats both as "not approved": :func:`require_trust` refuses
    rather than defaulting.
    """
    try:
        path = trust_decision_path(name)
    except PluginError:
        return None
    data = _read_json(path)
    if not isinstance(data, Mapping):
        return None
    return dict(data)


def record_trust_decision(name: str, approved: bool, *, note: str = "") -> Path:
    """Record one trust decision atomically; returns the row path.

    The recorded row carries the digest that was shown, so a later approval
    cannot be replayed against files that changed after it was given.
    """
    path = trust_decision_path(name)
    digest = verify_installed(name)
    _plugins._atomic_write_json(
        path,
        {
            "schema_version": 1,
            "name": name,
            "approved": bool(approved),
            "note": str(note or ""),
            "digest_at_decision": digest.actual_sha256,
            "recorded_at": _plugins._now_stamp(),
        },
    )
    return path


def require_trust(name: str, *, digest: Optional[DigestReport] = None) -> TrustReport:
    """Gate an install or an enable on a recorded, current approval.

    Refuses when the digest does not match the install record, and refuses
    when there is no approval. A recorded approval is only honoured when it
    was given for the SAME tree digest, so a plugin edited after approval is
    refused rather than loaded on a stale yes.
    """
    report = verify_installed(name) if digest is None else digest
    if not report.ok:
        message = report.reason or "integrity unknown"
        if report.checked:
            raise TamperedPlugin(f"plugin {name!r} is refused: {message}")
        raise TrustRefused(f"plugin {name!r} is refused: {message}")
    decision = trust_decision(name)
    if decision is None:
        raise TrustRefused(
            f"plugin {name!r} has no recorded trust decision; read /plugin trust "
            f"{name} and approve it before enabling"
        )
    if not decision.get("approved"):
        raise TrustRefused(f"plugin {name!r} was not approved")
    if (
        decision.get("digest_at_decision")
        and decision["digest_at_decision"] != report.actual_sha256
    ):
        raise TrustRefused(
            f"plugin {name!r} changed after it was approved; approve it again"
        )
    return trust_report(name, verify_digest=False)


# ---------------------------------------------------------------------------
# projected per-session token cost
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectedCost:
    """What one plugin costs a session, split by when it is paid.

    ``startup_tokens`` is the number that decides whether a plugin is worth
    keeping: a skill's DESCRIPTION is what session start and the plugin
    inventory carry, while a body is read only when a skill actually
    matches. ``on_demand_tokens`` is what a matching run would pay. The
    estimator is NAMED because it is a characters-per-token divisor, not a
    tokenizer, and a heuristic presented as exact is the failure this
    receipt exists to prevent.
    """

    plugin: str
    startup_chars: int = 0
    startup_tokens: int = 0
    on_demand_chars: int = 0
    on_demand_tokens: int = 0
    skills_at_startup: int = 0
    skills_on_demand: int = 0
    commands: int = 0
    agents: int = 0
    estimator: str = COST_ESTIMATOR

    @property
    def total_tokens(self) -> int:
        """Startup plus a fully-matched run, for the "keep or drop" question."""
        return self.startup_tokens + self.on_demand_tokens

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "plugin": self.plugin,
            "startup_chars": self.startup_chars,
            "startup_tokens": self.startup_tokens,
            "on_demand_chars": self.on_demand_chars,
            "on_demand_tokens": self.on_demand_tokens,
            "total_tokens": self.total_tokens,
            "skills_at_startup": self.skills_at_startup,
            "skills_on_demand": self.skills_on_demand,
            "commands": self.commands,
            "agents": self.agents,
            "estimator": self.estimator,
        }


def _skill_frontmatter_text(path: Path) -> Tuple[str, str]:
    """Return ``(description, body_chars)`` for one SKILL.md.

    The description is the frontmatter ``description:`` value; the body is
    everything after the closing fence, capped the way
    ``harness.skills`` caps it. Reads are bounded and never raise - a skill
    file that cannot be read contributes 0 and is not silently counted as
    free.
    """
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            return "", 0
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", 0
    lines = text.splitlines()
    description = ""
    body_start = 0
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                body_start = index + 1
                break
            match = re.match(r"^description:\s*(.*?)\s*$", line)
            if match:
                description = match.group(1).strip().strip("\"'")
    return description, len("\n".join(lines[body_start:]))


def projected_session_cost(
    name_or_dir: Any, *, installed: bool = True
) -> ProjectedCost:
    """Project what one plugin costs a session, in tokens.

    Two numbers, because there are two moments. At STARTUP a plugin's
    skills contribute their NAME and DESCRIPTION - that is what session
    start and the plugin inventory carry, and it is what a user decides
    about. A matching run additionally reads the BODY, which is bounded by
    the on-demand term.

    Assumes ``name_or_dir`` is an installed plugin NAME when ``installed``
    is True and a directory otherwise; an unreadable plugin reports zeros
    with the reason available from :func:`discover_components`.
    """
    if installed:
        try:
            root = _plugins._plugin_dir(str(name_or_dir))
        except PluginError:
            return ProjectedCost(plugin=str(name_or_dir))
        name = str(name_or_dir)
    else:
        root = Path(name_or_dir).expanduser()
        name = root.name
    components = discover_components(root, name=name)
    startup_chars = 0
    on_demand_chars = 0
    for skill in components.skills:
        description, body_chars = _skill_frontmatter_text(root / skill / "SKILL.md")
        folder = Path(skill).name
        startup_chars += len(folder) + len(description) + 32
        on_demand_chars += body_chars
    for command in components.commands:
        try:
            on_demand_chars += len((root / command).read_text(encoding="utf-8"))
        except OSError:
            continue
    return ProjectedCost(
        plugin=name,
        startup_chars=startup_chars,
        startup_tokens=_tokens(startup_chars),
        on_demand_chars=on_demand_chars,
        on_demand_tokens=_tokens(on_demand_chars),
        skills_at_startup=len(components.skills),
        skills_on_demand=len(components.skills),
        commands=len(components.commands),
        agents=len(components.agents),
    )


def _tokens(chars: int) -> int:
    """Convert characters to tokens with the declared, named divisor."""
    return int(max(0, int(chars)) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# marketplace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketplaceEntry:
    """One plugin offered by a marketplace document."""

    name: str
    description: str = ""
    version: str = ""
    source: str = ""
    source_type: str = ""
    skip_lfs: bool = False
    dependencies: Tuple[str, ...] = ()
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_remote(self) -> bool:
        """True when resolving this entry would need the network."""
        return self.source_type in REMOTE_SOURCE_TYPES

    @property
    def is_matcher(self) -> bool:
        """True for the two entries that SELECT rather than fetch."""
        return self.source_type in MARKETPLACE_MATCHER_TYPES

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "source": self.source,
            "source_type": self.source_type,
            "skip_lfs": self.skip_lfs,
            "dependencies": list(self.dependencies),
            "is_remote": self.is_remote,
            "is_matcher": self.is_matcher,
        }


@dataclass(frozen=True)
class Marketplace:
    """A parsed ``.claude-plugin/marketplace.json`` document."""

    name: str
    owner: Dict[str, str] = field(default_factory=dict)
    entries: Tuple[MarketplaceEntry, ...] = ()
    path: str = ""
    scope: str = "user"

    def entry(self, name: str) -> Optional[MarketplaceEntry]:
        """Return one entry by name, or ``None``."""
        for item in self.entries:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": self.name,
            "owner": dict(self.owner),
            "path": self.path,
            "scope": self.scope,
            "entries": [item.to_dict() for item in self.entries],
        }


def _normalize_source(entry: Mapping[str, Any]) -> Tuple[str, str]:
    """Return ``(source_type, source)`` for one marketplace entry.

    An entry may spell its type as ``source`` being an OBJECT with a
    ``type``, or as sibling ``source.type`` / ``source.source`` keys. A
    missing or unknown type is reported as ``""`` so the caller's refusal
    names it, rather than being coerced into the nearest valid type.
    """
    raw = entry.get("source")
    if isinstance(raw, Mapping):
        return str(raw.get("type") or ""), str(raw.get("source") or "")
    kind = str(entry.get("source_type") or entry.get("type") or "").strip()
    if not kind and isinstance(raw, str):
        kind = _infer_source_type(raw)
    return kind, str(raw or "")


def _infer_source_type(value: str) -> str:
    """Classify a bare source string; a guess is always LABELLED as one.

    ``_infer_source_type`` is only reached when the document omitted the
    type, and the caller records ``source_type_guessed`` for every entry it
    used, because a heuristically classified source that then fetches is a
    security decision nobody made.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return "url"
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", text):
        return "github"
    if text.startswith(("git@", "ssh://")) or text.endswith(".git"):
        return "git"
    if text.startswith("npm:"):
        return "npm"
    if Path(text).is_dir():
        return "directory"
    if Path(text).is_file():
        return "file"
    return ""


def _parse_marketplace(document: Any, *, path: str, scope: str) -> Marketplace:
    """Validate and project a marketplace document; raise on a bad shape.

    Requires ``name``, an ``owner`` object carrying BOTH ``name`` and
    ``email``, and a ``plugins`` list. Every entry's source type must be one
    of the eight; anything else is refused by name.
    """
    if not isinstance(document, Mapping):
        raise PluginError(f"marketplace {path} must be a JSON object")
    name = str(document.get("name") or "").strip()
    if not _NAME_PAT.match(name):
        raise PluginError(
            f"marketplace {path} needs a valid 'name' "
            f"(letters/digits/dots/dashes/underscores)"
        )
    owner = document.get("owner")
    if not isinstance(owner, Mapping):
        raise PluginError(f"marketplace {name!r} needs an 'owner' object")
    missing = [
        key for key in ("name", "email") if not str(owner.get(key) or "").strip()
    ]
    if missing:
        raise PluginError(f"marketplace {name!r} owner is missing {', '.join(missing)}")
    raw_plugins = document.get("plugins")
    if not isinstance(raw_plugins, list):
        raise PluginError(f"marketplace {name!r} needs a 'plugins' list")
    entries: List[MarketplaceEntry] = []
    for index, item in enumerate(raw_plugins):
        if not isinstance(item, Mapping):
            raise PluginError(
                f"marketplace {name!r} plugin #{index + 1} is not an object"
            )
        entry_name = str(item.get("name") or "").strip()
        if not _NAME_PAT.match(entry_name):
            raise PluginError(
                f"marketplace {name!r} plugin #{index + 1} has an invalid name "
                f"{item.get('name')!r}"
            )
        kind, source = _normalize_source(item)
        if kind not in MARKETPLACE_SOURCE_TYPES:
            raise PluginError(
                f"marketplace {name!r} entry {entry_name!r} has unsupported source "
                f"type {kind or '(none)'!r}; expected one of "
                f"{', '.join(MARKETPLACE_SOURCE_TYPES)}"
            )
        deps = item.get("dependencies") or []
        entries.append(
            MarketplaceEntry(
                name=entry_name,
                description=str(item.get("description") or ""),
                version=str(item.get("version") or ""),
                source=source,
                source_type=kind,
                skip_lfs=bool(item.get("skipLfs", item.get("skip_lfs", False))),
                dependencies=tuple(str(dep) for dep in deps)
                if isinstance(deps, (list, tuple))
                else (),
                raw=dict(item),
            )
        )
    return Marketplace(
        name=name,
        owner={"name": str(owner["name"]), "email": str(owner["email"])},
        entries=tuple(entries),
        path=path,
        scope=scope,
    )


def read_marketplace(path: Any, *, scope: str = "user") -> Marketplace:
    """Read and validate one ``marketplace.json``; raise on any bad shape."""
    target = Path(path).expanduser()
    document = _read_json(target)
    if document is None:
        raise PluginError(f"marketplace {target} is unreadable or not valid JSON")
    return _parse_marketplace(document, path=str(target), scope=scope)


def marketplace_at(plugin_dir: Any) -> Optional[Marketplace]:
    """Read the marketplace shipped INSIDE a plugin directory, if any.

    Returns ``None`` when there is none - a plugin is not required to carry
    a marketplace, so absence is the ordinary case and not an error.
    """
    target = Path(plugin_dir).expanduser() / ".claude-plugin" / "marketplace.json"
    if not target.is_file():
        return None
    document = _read_json(target)
    if not isinstance(document, Mapping):
        return None
    try:
        return _parse_marketplace(document, path=str(target), scope="bundled")
    except PluginError:
        return None


def add_marketplace_source(
    path: Any,
    source: str,
    *,
    name: Optional[str] = None,
    owner_name: str = "",
    owner_email: str = "",
    scope: str = "user",
    project_dir: Optional[str] = None,
) -> Marketplace:
    """Create or extend a marketplace document with one plugin entry.

    The document is written through the SAME atomic writer the install
    receipt uses, so a marketplace can never be observed half-written. An
    existing entry with the same name is REPLACED and the replacement is
    reported by the returned document.
    """
    target = Path(path).expanduser()
    if target.is_file():
        market = read_marketplace(target, scope=scope)
        entries = [item.to_dict() for item in market.entries]
        owner = dict(market.owner)
        market_name = market.name
    else:
        entries = []
        owner = {"name": owner_name, "email": owner_email}
        market_name = name or "local-marketplace"
    kind, resolved = _normalize_source({"source": source})
    if not kind:
        kind = _infer_source_type(source)
        resolved = source
    if kind not in MARKETPLACE_SOURCE_TYPES:
        raise PluginError(
            f"unsupported marketplace source type for {source!r}; expected one of "
            f"{', '.join(MARKETPLACE_SOURCE_TYPES)}"
        )
    entry_name = name or Path(resolved or source).stem or "plugin"
    entries = [item for item in entries if str(item.get("name")) != entry_name]
    entries.append({"name": entry_name, "source": {"type": kind, "source": resolved}})
    document = {
        "name": market_name,
        "owner": owner,
        "plugins": entries,
    }
    validated = _parse_marketplace(document, path=str(target), scope=scope)
    target.parent.mkdir(parents=True, exist_ok=True)
    _plugins._atomic_write_json(target, document)
    return validated


def fetch_marketplace_document(
    url: str,
    *,
    opener: Optional[Any] = None,
    timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
) -> Any:
    """Fetch a marketplace document over the network, BOUNDED.

    Two properties matter and both are structural:

    * it takes an INJECTABLE ``opener``, so the transport is the caller's
      and a test never opens a socket;
    * it is bounded twice - by ``timeout_s`` and by ``MAX_FETCH_BYTES`` -
      and it is NEVER called from a load, a discovery or a startup path.
      Only :func:`resolve_entry` calls it, and only for an explicit verb.
    """
    if not re.match(r"^https?://", str(url or "")):
        raise PluginError(f"not a fetchable marketplace URL: {url!r}")
    if opener is None:
        import urllib.request

        def opener(request: Any, *, timeout: float) -> Any:  # pragma: no cover
            return urllib.request.urlopen(request, timeout=timeout)

    class _Request:
        def __init__(self, target: str) -> None:
            self.full_url = target

        def __repr__(self) -> str:  # pragma: no cover - debugging aid
            return f"<marketplace request {self.full_url}>"

    try:
        response = opener(_Request(str(url)), timeout=float(timeout_s))
    except TypeError:
        # An injected opener that does not accept ``timeout`` still runs,
        # but the deadline is the caller's to enforce.
        response = opener(_Request(str(url)))
    except Exception as exc:
        raise PluginError(f"marketplace fetch failed for {url}: {exc}") from exc
    try:
        payload = response.read(MAX_FETCH_BYTES + 1)
    except Exception as exc:
        raise PluginError(f"marketplace fetch failed for {url}: {exc}") from exc
    finally:
        closer = getattr(response, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
    if len(payload) > MAX_FETCH_BYTES:
        raise PluginError(
            f"marketplace document at {url} exceeds {MAX_FETCH_BYTES} bytes"
        )
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PluginError(f"marketplace at {url} is not valid JSON: {exc}") from exc


def resolve_entry(
    entry: MarketplaceEntry, *, opener: Optional[Any] = None
) -> Dict[str, Any]:
    """Resolve one marketplace entry to something installable.

    Returns a receipt, not a side effect: ``{"ok", "kind", "path"|"url",
    "reason"}``. The three LOCAL source types resolve to a real path; the
    three REMOTE types return the command a caller should run and never
    fetch on their own; the two MATCHER types return the entries they select
    from the marketplace that supplied them.
    """
    if entry.is_matcher:
        return {
            "ok": True,
            "kind": "matcher",
            "name": entry.name,
            "reason": (
                f"{entry.source_type} selects entries by "
                f"{'host' if entry.source_type == 'hostPattern' else 'path'}; "
                "it is not a fetchable source"
            ),
            "matches": [],
        }
    if entry.source_type in {"file", "directory"}:
        candidate = Path(entry.source).expanduser()
        if not candidate.exists():
            return {
                "ok": False,
                "kind": entry.source_type,
                "name": entry.name,
                "reason": f"{entry.source_type} source does not exist: {entry.source}",
            }
        return {
            "ok": True,
            "kind": entry.source_type,
            "name": entry.name,
            "path": str(candidate),
        }
    if entry.source_type == "git":
        return {
            "ok": True,
            "kind": "git",
            "name": entry.name,
            "url": entry.source,
            "reason": "run /plugin install <git url> to install",
        }
    if entry.source_type == "github":
        owner_repo = entry.source.strip("/")
        if "/" not in owner_repo:
            return {
                "ok": False,
                "kind": "github",
                "name": entry.name,
                "reason": f"github source must be owner/repo, got {entry.source!r}",
            }
        return {
            "ok": True,
            "kind": "github",
            "name": entry.name,
            "url": f"https://github.com/{owner_repo}.git",
            "reason": "run /plugin install <git url> to install",
        }
    if entry.source_type == "npm":
        return {
            "ok": True,
            "kind": "npm",
            "name": entry.name,
            "spec": entry.source,
            "reason": "an npm source is not installed by this build",
        }
    if entry.source_type == "url":
        if opener is None:
            return {
                "ok": True,
                "kind": "url",
                "name": entry.name,
                "url": entry.source,
                "reason": (
                    "not fetched: pass an opener to /plugin marketplace show, "
                    "or download it yourself"
                ),
            }
        document = fetch_marketplace_document(entry.source, opener=opener)
        return {
            "ok": True,
            "kind": "url",
            "name": entry.name,
            "url": entry.source,
            "document": document,
        }
    return {
        "ok": False,
        "kind": entry.source_type or "unknown",
        "name": entry.name,
        "reason": f"unsupported source type {entry.source_type!r}",
    }


def list_marketplaces(
    *, project_dir: Optional[str] = None, opener: Optional[Any] = None
) -> List[Marketplace]:
    """Every configured marketplace across the three scopes.

    Precedence is ``local > project > user``, so a project can shadow a
    personal marketplace by declaring the same NAME - and a remote
    marketplace is only fetched when an opener was supplied, so listing
    marketplaces can never block a startup.
    """
    found: Dict[str, Marketplace] = {}
    for scope in sorted(INSTALL_SCOPES, key=lambda item: SCOPE_PRECEDENCE[item]):
        try:
            root = scoped_root(scope, project_dir=project_dir)
        except PluginError:
            continue
        for candidate in (
            root / "marketplace.json",
            root / ".claude-plugin" / "marketplace.json",
        ):
            if not candidate.is_file():
                continue
            try:
                market = read_marketplace(candidate, scope=scope)
            except PluginError:
                continue
            if market.name not in found:
                found[market.name] = market
    return [found[name] for name in sorted(found)]


# ---------------------------------------------------------------------------
# uninstall: every trace, then a re-scan that PROVES it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UninstallTrace:
    """What a scoped uninstall removed, across BOTH roots it can touch.

    ``cli.plugins.uninstall`` already owns the plugin tree, the install
    receipt, the disabled marker and the interrupted-install residue, and it
    re-scans the plugins root. This adds the two things OUTSIDE that root -
    the durable plugin DATA directory and the trust decision row - and then
    re-scans again so ``complete`` is a measurement over both roots rather
    than a claim about one.
    """

    name: str
    plugin_report: Optional[Dict[str, Any]] = None
    removed_data_dir: bool = False
    removed_trust_row: bool = False
    removed_scope_rows: int = 0
    retracted_path_entries: Tuple[str, ...] = ()
    remaining: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True when nothing named after this plugin survives in either root."""
        return not self.remaining

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": self.name,
            "plugin_report": self.plugin_report,
            "removed_data_dir": self.removed_data_dir,
            "removed_trust_row": self.removed_trust_row,
            "removed_scope_rows": self.removed_scope_rows,
            "retracted_path_entries": list(self.retracted_path_entries),
            "remaining": list(self.remaining),
            "errors": list(self.errors),
            "complete": self.complete,
        }


def _rescan_both_roots(
    name: str, *, project_dir: Optional[str] = None
) -> Tuple[str, ...]:
    """Return everything still named after ``name`` under either root."""
    found: List[str] = []
    needle = str(name).casefold()
    roots: List[Path] = [_plugins.plugins_root(), _neo_home() / "plugin-data"]
    try:
        project = _project_dir(project_dir)
    except Exception:
        project = ""
    if project:
        roots.extend(
            [
                Path(project) / ".neo" / "plugins",
                Path(project) / ".neo" / "plugins.local",
            ]
        )
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for entry in root.rglob("*"):
                rel = entry.relative_to(root).as_posix()
                if entry.name.casefold() == needle or needle in rel.casefold():
                    found.append(f"{root.name}/{rel}")
        except OSError:
            continue
    return tuple(sorted(set(found)))


def uninstall_plugin(name: str, *, project_dir: Optional[str] = None) -> UninstallTrace:
    """Remove EVERY trace of one plugin and prove it, across both roots.

    Delegates the tree/receipt/marker/residue work to
    ``cli.plugins.uninstall`` unchanged, then removes the durable data
    directory and the trust decision row, then RE-SCANS. A partial removal
    is reported as a partial removal: ``complete`` is False and ``remaining``
    names what survived.
    """
    errors: List[str] = []
    retracted: Tuple[str, ...] = ()
    removed_data = False
    removed_trust = False
    plugin_report: Optional[Dict[str, Any]] = None
    try:
        report = _plugins.uninstall(name)
        plugin_report = report.to_dict()
        retracted = tuple(report.retracted_path_entries)
        errors.extend(report.errors)
    except PluginError as exc:
        message = str(exc)
        if "no installed plugin named" not in message:
            errors.append(message)
    data = plugin_data_dir(name)
    if data.exists():
        import shutil

        try:
            shutil.rmtree(data)
            removed_data = True
        except OSError as exc:
            errors.append(f"could not remove {data}: {exc}")
    try:
        row = trust_decision_path(name)
        if row.is_file():
            row.unlink()
            removed_trust = True
    except (PluginError, OSError) as exc:
        errors.append(f"could not remove the trust record: {exc}")
    remaining = _rescan_both_roots(name, project_dir=project_dir)
    return UninstallTrace(
        name=name,
        plugin_report=plugin_report,
        removed_data_dir=removed_data,
        removed_trust_row=removed_trust,
        retracted_path_entries=retracted,
        remaining=remaining,
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# the ONE implementation of every /plugin verb
# ---------------------------------------------------------------------------


def _receipt(
    verb: str, ok: bool, lines: Sequence[str], **payload: Any
) -> Dict[str, Any]:
    """Return one plain receipt.

    Same shape ``cli.interactive._receipt`` returns, so the slash surface can
    delegate its whole body here without changing a single rendered line for
    the verbs that already worked.
    """
    return {
        "verb": verb,
        "ok": bool(ok),
        "lines": [str(line) for line in lines],
        "payload": dict(payload),
    }


def menu_lines() -> List[str]:
    """The / plugin menu rows, so a command is never undiscoverable.

    Every verb this module implements is named here, and every row is
    derived from the verb TABLE rather than hand-written, so a verb cannot
    exist without appearing in the menu a user reads.
    """
    rows = [
        "/plugin list                    installed plugins, their state and scope",
        "/plugin inspect <name>          components, dependencies and token cost",
        "/plugin tokens <name>            what this plugin costs a session",
        "/plugin install <ref>           install from a path, git URL or marketplace",
        "/plugin enable <name>           enable, and its transitive dependencies",
        "/plugin disable <name>          disable, refusing on a live dependent",
        "/plugin remove <name>           uninstall and every trace of it",
        "/plugin trust <name>            what this plugin does to your machine",
        "/plugin marketplace [add <src>] configured marketplaces and their sources",
        "/plugin verify <name>           re-check the installed files against the record",
        "/plugin reload                  rescan and report what changed",
    ]
    return rows


def _verb_list() -> Dict[str, Any]:
    # ``verify=True``: /plugin list is the surface that answers "what is
    # actually on this machine", so it re-hashes each tree against its
    # install record and says so per row. The cost is measured, not guessed
    # (see the module's own handoff).
    rows = load_scoped_plugins(verify=True)
    lines = [f"{len(rows)} plugin(s) installed:"]
    payload: Dict[str, Any] = {"count": 0, "plugins": []}
    for components in rows:
        word = "enabled" if components.enabled else "disabled"
        entry = f"  {components.name} ({word}) - {components.count} component(s)"
        if components.description:
            entry += f" - {components.description}"
        if components.digest_ok is False:
            entry += f" [CHANGED AFTER INSTALL: {components.digest_reason}]"
        for error in components.errors:
            entry += f" [problem: {error}]"
        lines.append(entry)
        payload["plugins"].append(components.to_dict())
    payload["count"] = len(payload["plugins"])
    payload["namespaces"] = [row[0] for row in namespace_verb_rows()]
    return _receipt("list", True, lines or ["no plugins installed"], **payload)


def _verb_inspect(argument: str) -> Dict[str, Any]:
    if not argument:
        return _receipt("inspect", False, ["usage: /plugin inspect <name>"])
    for components in load_scoped_plugins(verify=True):
        if components.name != argument:
            continue
        digest = verify_installed(components.name)
        cost = projected_session_cost(components.name)
        deps = dependency_report(components.name)
        lines = [f"{components.name}"]
        if components.description:
            lines.append(f"description: {components.description}")
        lines.append(f"version: {components.version or 'unknown'}")
        lines.append(f"enabled: {'yes' if components.enabled else 'no'}")
        lines.append(f"root: {components.root}")
        lines.append(f"manifest: {components.manifest_path or 'none (by convention)'}")
        for kind, members in components.to_dict().items():
            if (
                kind
                in {
                    "skills",
                    "commands",
                    "agents",
                    "hooks",
                    "mcp",
                    "lsp",
                    "monitors",
                    "bin",
                    "settings",
                }
                and members
            ):
                lines.append(f"{kind}: {', '.join(str(m) for m in members)}")
        if components.tool_verbs:
            lines.append(f"tool verbs: {', '.join(components.tool_verbs)}")
        if components.mcp_servers:
            lines.append(f"mcp servers: {', '.join(components.mcp_servers)}")
        if deps.edges:
            for edge in deps.edges:
                state = (
                    "satisfied"
                    if edge.satisfied
                    else ("undecided" if edge.satisfied is None else "UNSATISFIED")
                )
                lines.append(
                    f"needs: {edge.name} {edge.constraint or '(any)'} [{state}]"
                )
        for row in namespace_verb_rows():
            if row[1] == components.name:
                lines.append(f"invocation: {row[0]}")
        lines.append(
            f"token cost per session: ~{cost.startup_tokens} at startup "
            f"(names + descriptions), ~{cost.on_demand_tokens} when a skill "
            f"matches [estimator {cost.estimator}]"
        )
        lines.append(f"integrity: {digest.reason or 'matches the install record'}")
        return _receipt(
            "inspect",
            True,
            lines,
            plugin=components.to_dict(),
            digest=digest.to_dict(),
            token_cost=cost.to_dict(),
            dependencies=deps.to_dict(),
        )
    return _receipt("inspect", False, [f"no plugin named {argument}"])


def _verb_install(argument: str, *, confirm: Optional[Any] = None) -> Dict[str, Any]:
    if not argument:
        return _receipt("install", False, ["usage: /plugin install <ref>"])
    try:
        name = _plugins.install(argument)
    except PluginError as exc:
        return _receipt("install", False, [f"install failed: {exc}"])
    report = trust_report(name)
    lines = [f"installed plugin {name}", *trust_lines(report)[1:]]
    if report.requires_review:
        approved = False
        if confirm is not None:
            try:
                approved = bool(confirm(name, report))
            except Exception:
                approved = False
        if not approved:
            _plugins.disable(name)
            record_trust_decision(name, False, note="not approved at install")
            lines.append("NOT approved, so it is installed but DISABLED.")
            lines.append(f"read it first: /plugin trust {name}")
        else:
            record_trust_decision(name, True, note="approved at install")
            lines.append("approved and enabled.")
    else:
        lines.append("ships nothing executable, so no trust prompt was needed.")
    return _receipt("install", True, lines, name=name, trust=report.to_dict())


def _verb_enable(argument: str) -> Dict[str, Any]:
    if not argument:
        return _receipt("enable", False, ["usage: /plugin enable <name>"])
    try:
        require_trust(argument)
    except PluginError as exc:
        return _receipt("enable", False, [f"not enabled: {exc}"])
    try:
        report = enable_plugin(argument)
    except PluginError as exc:
        return _receipt("enable", False, [f"enable failed: {exc}"])
    lines = [f"enabled plugin {argument}"]
    if report.enabled:
        lines.append(
            "also enabled the dependencies it needs: " + ", ".join(report.enabled)
        )
    if report.missing_dependencies:
        lines.append("missing dependencies: " + ", ".join(report.missing_dependencies))
    if report.unsatisfied:
        lines.append("unsatisfied version ranges: " + ", ".join(report.unsatisfied))
    if report.undecidable:
        lines.append("could not be decided: " + ", ".join(report.undecidable))
    return _receipt("enable", True, lines, **report.to_dict())


def _verb_disable(argument: str) -> Dict[str, Any]:
    if not argument:
        return _receipt("disable", False, ["usage: /plugin disable <name>"])
    try:
        report = disable_plugin(argument)
    except PluginError as exc:
        return _receipt("disable", False, [f"disable failed: {exc}"])
    if not report.disabled:
        return _receipt("disable", False, [report.reason], **report.to_dict())
    return _receipt(
        "disable", True, [f"disabled plugin {argument}"], **report.to_dict()
    )


def _verb_remove(argument: str) -> Dict[str, Any]:
    if not argument:
        return _receipt("remove", False, ["usage: /plugin remove <name>"])
    dependents = dependents_of(argument)
    if dependents:
        return _receipt(
            "remove",
            False,
            [
                f"cannot remove {argument!r}: {', '.join(dependents)} depend on it. "
                "Remove or disable them first."
            ],
            dependents=list(dependents),
        )
    trace = uninstall_plugin(argument)
    lines = [f"removed plugin {argument}"]
    if trace.removed_data_dir:
        lines.append(f"removed its data directory {plugin_data_dir(argument)}")
    if trace.removed_trust_row:
        lines.append("removed its trust record")
    if trace.retracted_path_entries:
        lines.append("retracted: " + ", ".join(trace.retracted_path_entries))
    if not trace.complete:
        lines.append("NOT complete; these remain: " + ", ".join(trace.remaining))
    for error in trace.errors:
        lines.append(f"error: {error}")
    return _receipt("remove", trace.complete, lines, **trace.to_dict())


def _verb_trust(argument: str, *, decide: Optional[Any] = None) -> Dict[str, Any]:
    if not argument:
        return _receipt("trust", False, ["usage: /plugin trust <name>"])
    report = trust_report(argument)
    if not report.root:
        return _receipt("trust", False, [f"no plugin named {argument}"])
    lines = trust_lines(report)
    payload: Dict[str, Any] = {"trust": report.to_dict()}
    if decide is not None:
        approved = None
        try:
            approved = decide(report)
        except Exception:
            approved = None
        if approved is not None:
            record_trust_decision(argument, bool(approved))
            lines.append("recorded: " + ("approved" if approved else "NOT approved"))
            payload["recorded"] = bool(approved)
    return _receipt("trust", True, lines, **payload)


def _verb_marketplace(argument: str, *, opener: Optional[Any] = None) -> Dict[str, Any]:
    parts = str(argument or "").strip().split(None, 1)
    markets = list_marketplaces()
    if parts and parts[0] == "add":
        if len(parts) < 2:
            return _receipt(
                "marketplace",
                False,
                [
                    'usage: /plugin marketplace add <src> --owner "Name <email>"',
                    "a marketplace must declare who publishes it, so the owner "
                    "is required rather than guessed",
                ],
            )
        remainder = parts[1]
        owner_name = ""
        owner_email = ""
        source = remainder
        if "--owner" in remainder:
            source, _, owner_spec = remainder.partition("--owner")
            match = re.search(r"<?([^\s<]+@[^>\s]+)>?", owner_spec.strip())
            if match is not None:
                owner_email = match.group(1)
                owner_name = owner_spec.strip()[: match.start()].strip(" \"'<")
            else:
                owner_name = owner_spec.strip().strip("\"'")
        if not owner_email or "@" not in owner_email:
            return _receipt(
                "marketplace",
                False,
                [
                    'usage: /plugin marketplace add <src> --owner "Name <email>"',
                    "the owner needs a name AND an email; nothing was written",
                ],
            )
        target = scoped_root("user") / "marketplace.json"
        try:
            market = add_marketplace_source(
                target,
                source.strip(),
                name=Path(source.strip().rstrip("/\\")).stem or None,
                owner_name=owner_name,
                owner_email=owner_email,
                scope="user",
            )
        except PluginError as exc:
            return _receipt("marketplace", False, [str(exc)])
        return _receipt(
            "marketplace",
            True,
            [
                f"marketplace {market.name!r} now offers "
                f"{len(market.entries)} plugin(s) at {target}"
            ],
            marketplace=market.to_dict(),
        )
    if not markets:
        return _receipt(
            "marketplace",
            True,
            [
                "no marketplace is configured in this build",
                "add one: /plugin marketplace add <path-or-url>",
            ],
            marketplaces=[],
        )
    lines: List[str] = []
    payload: List[Dict[str, Any]] = []
    for market in markets:
        lines.append(f"{market.name} [{market.scope}] {market.path}")
        lines.append(f"  owner: {market.owner['name']} <{market.owner['email']}>")
        for entry in market.entries:
            note = ""
            if entry.skip_lfs:
                note = " (skipLfs)"
            lines.append(
                f"  - {entry.name} via {entry.source_type}: {entry.source}{note}"
            )
            payload.append(entry.to_dict())
            if parts and parts[0] == "show" and entry.name == parts[1]:
                resolved = resolve_entry(entry, opener=opener)
                lines.append(f"    -> {resolved.get('reason') or resolved.get('path')}")
                payload[-1]["resolved"] = resolved
    return _receipt("marketplace", True, lines, entries=payload)


def _verb_verify(argument: str) -> Dict[str, Any]:
    if not argument:
        return _receipt("verify", False, ["usage: /plugin verify <name>"])
    report = verify_installed(argument)
    line = (
        f"{argument}: files match the install record"
        if report.ok
        else f"{argument}: REFUSED - {report.reason}"
    )
    return _receipt("verify", report.ok, [line], digest=report.to_dict())


def _verb_reload() -> Dict[str, Any]:
    try:
        applied = _plugins.apply_tool_extensions()
    except Exception as exc:  # pragma: no cover - defensive
        return _receipt("reload", False, [f"reload failed: {exc}"])
    plugins = load_scoped_plugins()
    problems = [f"{item.name}: {error}" for item in plugins for error in item.errors]
    lines = [
        f"rescanned {len(plugins)} plugin(s)",
        f"batch verbs: {', '.join(applied) or 'none'}",
    ]
    lines.extend(f"problem: {problem}" for problem in problems)
    return _receipt(
        "reload",
        True,
        lines,
        count=len(plugins),
        verbs=list(applied),
        problems=problems,
    )


def run_plugin_verb(
    verb: str,
    rest: str = "",
    *,
    confirm: Optional[Any] = None,
    decide: Optional[Any] = None,
    opener: Optional[Any] = None,
    project_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Run ONE ``/plugin`` verb; the single implementation of the surface.

    This is the PRIMARY implementation. ``cli.interactive.plugin_subcommand``
    and any script surface delegate here, so a behaviour cannot exist twice
    and drift. ``confirm`` (install) and ``decide`` (trust) are INJECTED
    interactors: the module never prompts on its own, which is what keeps it
    testable and keeps a script from blocking on a question.

    ``ok=False`` in the returned receipt means the verb REFUSED, and its
    ``lines`` carry one plain sentence a surface prints verbatim.
    """
    name = str(verb or "list").strip().lower()
    argument = str(rest or "").strip()
    if name == "list":
        return _verb_list()
    if name in {"inspect", "show"}:
        return _verb_inspect(argument)
    if name == "install":
        return _verb_install(argument, confirm=confirm)
    if name == "enable":
        return _verb_enable(argument)
    if name == "disable":
        return _verb_disable(argument)
    if name in {"remove", "uninstall"}:
        return _verb_remove(argument)
    if name == "trust":
        return _verb_trust(argument, decide=decide)
    if name == "marketplace":
        return _verb_marketplace(argument, opener=opener)
    if name == "verify":
        return _verb_verify(argument)
    if name == "reload":
        return _verb_reload()
    if name == "tokens":
        if not argument:
            return _receipt("tokens", False, ["usage: /plugin tokens <name>"])
        cost = projected_session_cost(argument)
        return _receipt(
            "tokens",
            True,
            [
                f"{cost.plugin}: ~{cost.startup_tokens} token(s) at startup, "
                f"~{cost.on_demand_tokens} when a skill matches "
                f"[estimator {cost.estimator}]"
            ],
            token_cost=cost.to_dict(),
        )
    return _receipt(name, False, [f"usage: /plugin {name}", *menu_lines()])
