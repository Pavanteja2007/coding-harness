"""Neo user settings — TWO-TIER config (global + project), matching the
Claude Code pattern (two-tier config round, 2026-09-13).

Directories (created by Neo's own first-run flow and the `neo config`
command — never by installers):

    Global   Windows: %APPDATA%\\neo\\settings.toml
             POSIX:   ~/.config/neo/settings.toml (XDG_CONFIG_HOME honored)
             (the file named by $NEO_CONFIG always wins for the global tier)
    Project  <repo>/.neo/settings.toml          safe to commit (no secrets)
             <repo>/.neo/settings.local.toml    personal overrides, auto-
                                                 added to the repo's
                                                 .gitignore when Neo
                                                 creates it (Claude Code's
                                                 settings.local.json
                                                 pattern)
             <repo>/.neo/commands/<name>.md     project slash commands
             <repo>/.neo/skills/<name>/SKILL.md project skills
             (the whole project layout is scaffolded with examples on
             the first `neo` run inside a git repo — never overwritten,
             never outside a repo; `neo config init-project` is the
             explicit form)

The project tier is found by walking up from the CWD for a `.neo/`
directory ($NEO_PROJECT_DIR overrides — points AT the .neo dir).

Precedence, highest to lowest:

    1. explicit CLI flags / session state (the caller's own dict)
    2. environment: NEO_MODEL, NEO_PROVIDER, NEO_BASE_URL, NEO_API_BASE,
       NEO_API_KEY
    3. project .neo/settings.local.toml
    4. project .neo/settings.toml
    5. global settings.toml ($NEO_CONFIG or the platform path)
    6. legacy ~/.neo/config.toml — read ONLY when the new global file is
       missing (pre-two-tier installs; `neo config path` shows the
       migration hint)
    7. built-in defaults (applied by the harness's config merge, not here)

Schema: all keys optional; unknown keys pass through untouched (same
philosophy as harness.get_config — future/other-terminal knobs work
without this module changing). One nested `[neo]` table is accepted for
grouping. Keys this module itself interprets: model, provider,
base_url (alias of runtime's api_base), api_base, api_key,
budget_cap_usd, max_retries, plan_preview, log_verbosity, log_root.

TOML parse errors NEVER crash the CLI: a broken file is reported once on
stderr and ignored (defaults apply) — a settings file is a convenience,
not a load-bearing input.
"""

from __future__ import annotations

import contextlib
import fnmatch
import itertools
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Keys this module itself interprets (anything else passes through to the
# harness/router config merge untouched).
_NEO_KEYS = {
    "model",
    "provider",
    "base_url",
    "api_base",
    "api_key",
    "budget_cap_usd",
    "max_retries",
    "plan_preview",
    "log_verbosity",
    "display_label",
    "label",
    "health_check",
    "profile",
    "provider_profile",
    "provider_profiles",
}

# Keys that must stay strings even when they look numeric (model "3.5").
_STRING_KEYS = {
    "model",
    "provider",
    "base_url",
    "api_base",
    "api_key",
    "log_verbosity",
    "display_label",
    "label",
}

# Structured (dict-valued) keys the CLI's `config set` refuses to write.
_STRUCTURED_KEYS = {"model_tiers", "difficulty_llm", "provider_profiles"}

_PROFILE_NAME_PAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PROFILE_FIELDS = {
    "provider",
    "model",
    "base_url",
    "api_base",
    "api_key",
    "display_label",
    "label",
    "health_check",
}
_PROFILE_ALIASES = {
    "api_base": "base_url",
    "base_url": "api_base",
    "display_label": "label",
    "label": "display_label",
}

# Env var -> settings key (tier 2 of the precedence chain).
_ENV_KEYS = {
    "NEO_MODEL": "model",
    "NEO_PROVIDER": "provider",
    "NEO_BASE_URL": "base_url",
    "NEO_API_BASE": "api_base",
    "NEO_API_KEY": "api_key",
    "NEO_DISPLAY_LABEL": "display_label",
    "NEO_LABEL": "label",
    "NEO_HEALTH_CHECK": "health_check",
    "NEO_PROVIDER_PROFILE": "provider_profile",
}

_LOCAL_GITIGNORE_ENTRY = ".neo/settings.local.toml"
_CONNECTORS_LOCAL_GITIGNORE_ENTRY = ".neo/connectors.local.toml"
_LOCAL_GITIGNORE_ENTRIES = (
    _LOCAL_GITIGNORE_ENTRY,
    _CONNECTORS_LOCAL_GITIGNORE_ENTRY,
)

_STARTER_HEADER = (
    "# Neo settings — `neo config list` shows the effective values.\n"
    "# Precedence (highest first): CLI flags > env (NEO_MODEL, NEO_BASE_URL,\n"
    "# NEO_API_KEY, NEO_PROVIDER, ...) > .neo/settings.local.toml >\n"
    "# .neo/settings.toml > this file.\n"
)
_STARTER_GLOBAL = (
    _STARTER_HEADER
    + "#\n"
    + "# Keys: model, provider, base_url, api_key, display_label, health_check,\n"
    + "# budget_cap_usd, max_retries, plan_preview, log_verbosity, log_root.\n"
    + "# Point Neo at any OpenAI-compatible router with\n"
    + "#   neo config set base_url https://my-router.example.com/v1\n"
    + "#   neo config set model <any model name the router serves>\n"
)
_STARTER_PROJECT = (
    _STARTER_HEADER
    + "#\n"
    + "# Safe to commit — keep secrets in settings.local.toml or the\n"
    + "# global file, never here.\n"
)
_STARTER_LOCAL = (
    "# Neo local settings — personal overrides for this repo only.\n"
    "# This file is git-ignored (never committed); put secrets here,\n"
    '# e.g. api_key = "..." — `neo config list` shows effective values.\n'
)

# Example project command (cli/commands.py: one .md template, $ARGUMENTS
# is the single substitution slot). The name must not collide with a
# built-in slash command (see BUILTIN_SLASH_COMMANDS) or the file is
# dead on arrival — /fix is not a built-in.
_EXAMPLE_COMMAND_NAME = "fix"
_EXAMPLE_COMMAND_MD = (
    "# /fix — reusable fix workflow for this repo\n"
    "\n"
    "Fix the following, then verify with the repo's own test command:\n"
    "\n"
    "$ARGUMENTS\n"
    "\n"
    "Keep the change minimal and leave unrelated code alone.\n"
)

# Example project skill (harness/skills.py: <name>/SKILL.md with
# frontmatter name/description + instruction body).
_EXAMPLE_SKILL_DIR = "code-review"
_EXAMPLE_SKILL_MD = (
    "---\n"
    "name: code-review\n"
    "description: How to review a change in this repo before calling it done.\n"
    "---\n"
    "\n"
    "# Code review\n"
    "\n"
    "Before calling a change done: re-read the diff, run the repo's own\n"
    "test command, and check that no unrelated files were touched.\n"
)
_EXAMPLE_CONNECTORS_TOML = (
    "# Neo MCP connector examples.\n"
    "# Add servers under [mcp_servers]; keep secrets in the local file.\n"
    "# [mcp_servers]\n"
    '# docs = "python -m mcp_server"\n'
)
_EXAMPLE_CONNECTORS_LOCAL_TOML = (
    "# Personal MCP connector overrides. This file is git-ignored.\n"
    "# [mcp_servers]\n"
    '# docs = "python -m mcp_server --workspace ."\n'
)


# ---------------------------------------------------------------------------
# Tier paths
# ---------------------------------------------------------------------------


def global_settings_path() -> Path:
    """The global settings file: $NEO_CONFIG wins, else the platform
    location (%APPDATA%\\neo on Windows, ~/.config/neo elsewhere).

    A file under the PREVIOUS directory name is returned when the current
    one does not exist, so an upgraded install keeps reading the settings it
    already has instead of silently reverting to defaults. The current
    directory always wins when both exist, and the fallback never writes
    into the legacy location.
    """
    for var in ("NEO_CONFIG", "VEX_CONFIG"):
        env = os.environ.get(var)
        if env:
            return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base).expanduser() if base else Path.home() / ".config"
    current = root / "neo" / "settings.toml"
    try:
        from shared.brand import LEGACY_HOME_DIRNAME
    except Exception:
        return current
    legacy = root / LEGACY_HOME_DIRNAME / "settings.toml"
    try:
        if not current.exists() and legacy.is_file():
            return legacy
    except OSError:
        pass
    return current


def legacy_settings_path() -> Path:
    """The pre-two-tier single config file (~/.neo/config.toml). Read only
    as a fallback when the new global file is missing; $NEO_LEGACY_CONFIG
    overrides (test isolation). A ``.vex/config.toml`` from the previous
    product name is read too, and wins over a ``.neo/config.toml`` that does
    not exist."""

    for var in ("NEO_LEGACY_CONFIG", "VEX_LEGACY_CONFIG"):
        env = os.environ.get(var)
        if env:
            return Path(env).expanduser()
    current = Path.home() / ".neo" / "config.toml"
    try:
        from shared.brand import LEGACY_HOME_DIRNAME
    except Exception:
        return current
    legacy = Path.home() / f".{LEGACY_HOME_DIRNAME}" / "config.toml"
    try:
        if not current.exists() and legacy.is_file():
            return legacy
    except OSError:
        pass
    return current


def project_settings_dir(start: Optional[Path] = None) -> Optional[Path]:
    """The project's .neo/ directory: $NEO_PROJECT_DIR wins (authoritative
    even when not yet on disk — `neo config set --tier project` creates
    it), else the nearest ancestor of the CWD (or `start`) that contains
    one. None when no project config exists on the chain.

    **The walk is bounded to the enclosing git repository**, and does
    nothing at all when there is no git root. A project state directory
    belongs to a repository; a directory ABOVE the repository root is a
    user-level directory, and treating one as a project's settings means
    every unrelated project on the machine reads the same file. The
    unbounded form was reachable in practice because a very common setup is
    a dotfiles git repository at ``$HOME``: a session in any temp directory
    then resolved its project tier to ``~/.neo``. An explicit
    ``$NEO_PROJECT_DIR`` is the way to opt into a non-repository location,
    and it is honoured above this bound.

    The bound is the loop's STOP condition, not a "is there a repo" test
    performed and then walked past — that is the same unbounded walk with
    extra steps, and the ``$HOME`` case above is exactly what it finds.

    A directory under the PREVIOUS name (``.vex/``) is also found, so a
    repository that has not renamed its own state directory keeps its
    project settings, custom commands, skills and connectors. ``.neo/`` is
    preferred whenever both exist: a stale legacy directory must not win
    over the current one, or a user who created ``.neo/`` by accident would
    keep reading the old one forever. Both branches obey the same bound, so
    the current and the previous name can never resolve to different
    scopes.

    The legacy branch is a READ fallback. It never migrates, renames or
    writes into ``.vex/`` — see :func:`neo_migration_notice`.
    """
    for var in ("NEO_PROJECT_DIR", "VEX_PROJECT_DIR"):
        env = os.environ.get(var)
        # A blank value is an ABSENCE, not a directory named " ". Measured:
        # `NEO_PROJECT_DIR="   "` resolved the whole project tier to a
        # literal three-space directory name, so an export that padded the
        # value silently sent every settings read somewhere that cannot
        # exist. This is the same rule `shared.brand.apply_legacy_env`
        # already applies to the generic `VEX_*` -> `NEO_*` env rename.
        #
        # A value whose basename is empty (".", "..", "/") is malformed for a
        # variable documented to name the SETTINGS directory: it names a
        # location but no directory name. Measured: `NEO_PROJECT_DIR="."`
        # resolved the project tier to the current working directory — which
        # is almost never a settings directory — instead of degrading to repo
        # detection. Malformed degrades; it does not get to answer.
        if not env or not env.strip():
            continue
        candidate = Path(env.strip()).expanduser()
        if not candidate.name or candidate.name in (".", ".."):
            continue
        return candidate
    d = Path(start) if start is not None else Path.cwd()
    legacy_name = _legacy_project_dirname()
    names = (".neo",) if not legacy_name else (".neo", legacy_name)
    repo = find_git_root(d)
    if repo is None:
        return None
    for cand in (d, *d.parents):
        for name in names:
            if (cand / name).is_dir():
                return cand / name
        if cand == repo:  # inclusive stop: never walk above the repository
            break
    return None


def _legacy_project_dirname() -> str:
    """``.vex`` when the brand module is importable, else ``""``.

    An empty string disables the legacy branch rather than guessing: a
    fallback that cannot be told from the real name is a fallback that will
    eventually find the wrong directory.
    """
    try:
        from shared.brand import legacy_project_dirname
    except Exception:
        return ""
    try:
        return str(legacy_project_dirname())
    except Exception:
        return ""


def neo_migration_notice(d: Optional[Path]) -> str:
    """One line naming the legacy directory this project is still using.

    Empty when the project is on ``.neo/`` (or has no project directory at
    all), so a caller can print the return value unconditionally.
    """
    legacy_name = _legacy_project_dirname()
    if not d or not legacy_name or d.name != legacy_name:
        return ""
    return (
        f"note: this repository's agent state is still in `{legacy_name}/`. "
        f"Rename it to `.neo/` when convenient; both are read until the old "
        f"name is removed."
    )


def project_settings_path(start: Optional[Path] = None) -> Optional[Path]:
    """<project>/.neo/settings.toml (None when no project dir found)."""
    d = project_settings_dir(start)
    return d / "settings.toml" if d else None


def local_settings_path(start: Optional[Path] = None) -> Optional[Path]:
    """<project>/.neo/settings.local.toml (None when no project dir)."""
    d = project_settings_dir(start)
    return d / "settings.local.toml" if d else None


def tier_path(tier: str, start: Optional[Path] = None) -> Path:
    """Resolve a tier name ("global" | "project" | "local") to its file.

    For the project tiers this is the WRITE view: when no .neo/ is found
    by walking up, the target is <CWD>/.neo/... (set creates it), rather
    than None.
    """
    if tier == "global":
        return global_settings_path()
    if tier in ("project", "project-tier"):
        d = project_settings_dir(start)
        if d is None:
            base = Path(start) if start is not None else Path.cwd()
            d = base / ".neo"
        return d / "settings.toml"
    if tier in ("local", "project-local"):
        d = project_settings_dir(start)
        if d is None:
            base = Path(start) if start is not None else Path.cwd()
            d = base / ".neo"
        return d / "settings.local.toml"
    raise ValueError(f"unknown config tier: {tier!r}")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _parse_toml(text: str) -> Optional[Dict[str, Any]]:
    """Parse TOML via tomllib (3.11+) or tomli (3.10; a dependency of the
    sandbox image already). Returns None on parse failure (TOMLDecodeError
    subclasses ValueError in both parsers)."""
    parsers = []
    try:
        import tomllib  # Python 3.11+

        parsers.append(tomllib)
    except ModuleNotFoundError:
        try:
            import tomli

            parsers.append(tomli)
        except ImportError:
            return None  # no TOML parser at all: degrade, never crash
    for parser in parsers:
        try:
            return parser.loads(text)
        except ValueError:  # TOMLDecodeError
            return None
    return None


def _type_ok(key: str, value: Any) -> bool:
    if key in _STRING_KEYS:
        return isinstance(value, str) and bool(value.strip())
    if key in ("budget_cap_usd",):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if key in ("max_retries",):
        return isinstance(value, int) and not isinstance(value, bool)
    if key in ("plan_preview", "health_check"):
        return isinstance(value, bool)
    if key == "provider_profile":
        return (isinstance(value, str) and bool(value.strip())) or isinstance(
            value, dict
        )
    if key == "provider_profiles":
        return isinstance(value, dict)
    return True


def _warn(msg: str) -> None:
    """One-shot stderr warning (plain print — ui import would be circular
    here for callers that import this module before ui)."""
    print(f"neo config: {msg}", file=sys.stderr)


# Warn-once registry: a broken settings file is reported the FIRST time
# it is read per process, then stays silent (the chain is re-read on
# every `config list` key-lookup — without this one broken file spams
# a warning per key). Keyed by path + failure kind.
_WARNED: set = set()


def _warn_once(key: str, msg: str) -> None:
    """Warn at most once per process for `key` (best-effort)."""
    if key in _WARNED:
        return
    _WARNED.add(key)
    _warn(msg)


def _read_settings(p: Path) -> Optional[Dict[str, Any]]:
    """Parse ONE settings file into a flat dict. Returns None when the
    file exists but is unreadable/broken (missing files are {} — the
    normal case); wrong-typed known keys are dropped with a warning."""
    if not p.is_file():
        return {}
    try:
        # utf-8-sig: tolerates the BOM Windows editors (PowerShell, Notepad)
        # prepend — a config file is user-editable, so BOMs must not break it
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        _warn_once(
            f"{p}|unreadable", f"cannot read {p}: {exc} — ignoring settings file"
        )
        return None
    data = _parse_toml(text)
    if data is None:
        _warn_once(f"{p}|invalid", f"{p} is not valid TOML — ignoring settings file")
        return None
    if not isinstance(data, dict):
        _warn_once(
            f"{p}|top-level", f"{p}: top level must be a table — ignoring settings file"
        )
        return None

    out: Dict[str, Any] = {}
    # one nested table is allowed for grouping: [neo] ...
    if "neo" in data and isinstance(data["neo"], dict):
        merged = dict(data["neo"])
        merged.update({k: v for k, v in data.items() if k != "neo"})
        data = merged

    for key, value in data.items():
        if key in _NEO_KEYS and not _type_ok(key, value):
            _warn_once(
                f"{p}|type:{key}",
                f"{p}: {key} has wrong type ({type(value).__name__}) — dropping it",
            )
            continue
        out[key] = value
    return out


def load_neo_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read ONE settings file (default: the global tier) into a flat dict;
    {} when absent/unreadable.

    Assumes: a missing file is the NORMAL case (fresh install); a present
    but unparseable file is reported to stderr once and skipped, never
    raised — the CLI must stay usable with a broken config.
    """
    p = path if path is not None else global_settings_path()
    data = _read_settings(p)
    return data if data is not None else {}


def settings_chain(
    start: Optional[Path] = None,
) -> List[Tuple[str, Path, Dict[str, Any]]]:
    """The existing settings files, LOW->HIGH merge order.

    Returns (label, path, parsed-dict) triples for every tier that has a
    file on disk (legacy only included when the new global file is
    missing — pure back-compat). `merged_settings` folds these in order.
    """
    tiers: List[Tuple[str, Path, Dict[str, Any]]] = []
    gp = global_settings_path()
    if gp.is_file():
        tiers.append(("global", gp, load_neo_config(gp)))
    elif legacy_settings_path().is_file():
        lp = legacy_settings_path()
        tiers.append(("legacy", lp, load_neo_config(lp)))
    pd = project_settings_dir(start)
    if pd is not None:
        ps = pd / "settings.toml"
        ls = pd / "settings.local.toml"
        if ps.is_file():
            tiers.append(("project", ps, load_neo_config(ps)))
        if ls.is_file():
            tiers.append(("project-local", ls, load_neo_config(ls)))
    return tiers


def _merge_settings_data(base: Dict[str, Any], incoming: Dict[str, Any]) -> None:
    """Merge one settings tier, deep-merging named provider profiles."""
    for key, value in incoming.items():
        if key == "provider_profiles" and isinstance(value, dict):
            merged = (
                dict(base.get(key) or {}) if isinstance(base.get(key), dict) else {}
            )
            for name, profile in value.items():
                if isinstance(profile, dict) and isinstance(merged.get(name), dict):
                    combined = dict(merged[name])
                    combined.update(profile)
                    merged[name] = combined
                else:
                    merged[name] = profile
            base[key] = merged
        else:
            base[key] = value


def merged_settings(start: Optional[Path] = None) -> Dict[str, Any]:
    """All file tiers folded together (legacy < global < project < local).

    This is tiers 3-6 of the precedence chain WITHOUT env vars — env is
    applied by effective_settings()/apply_config_defaults so explicit
    caller values keep a single uniform place to win. Named provider
    profile tables merge by profile and field so a project can override
    one model without discarding the global profile's endpoint or key.
    """
    out: Dict[str, Any] = {}
    for _label, _p, data in settings_chain(start):
        _merge_settings_data(out, data)
    return out


def env_overrides() -> Dict[str, Any]:
    """Settings from NEO_* environment variables (tier 2). Empty-string
    values are ignored (an exported-but-blank var must not blank a file)."""
    out: Dict[str, Any] = {}
    for var, key in _ENV_KEYS.items():
        val = os.environ.get(var)
        if val:  # non-empty only
            out[key] = coerce_value(key, val) if key == "health_check" else val
    if "display_label" in out and "label" not in out:
        out["label"] = out["display_label"]
    return out


def _provider_env_name(provider: Any, base_url: Any = None) -> Optional[str]:
    """Return the active standard provider credential variable, if any."""
    try:
        host = (urlsplit(str(base_url or "")).hostname or "").lower()
    except ValueError:
        host = ""
    by_host = {
        "openrouter.ai": "OPENROUTER_API_KEY",
        "api.tokenrouter.com": "TOKENROUTER_API_KEY",
        "tokenrouter.com": "TOKENROUTER_API_KEY",
        "agentrouter.org": "AGENTROUTER_API_KEY",
        "ollama.com": "OLLAMA_API_KEY",
    }
    if host in by_host:
        return by_host[host]
    name = str(provider or "").strip().lower()
    candidates = {
        "openai": ("OPENAI_API_KEY",),
        "anthropic": ("ANTHROPIC_API_KEY",),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "openrouter": ("OPENROUTER_API_KEY",),
        "tokenrouter": ("TOKENROUTER_API_KEY",),
        "agentrouter": ("AGENTROUTER_API_KEY",),
        "ollama": ("OLLAMA_API_KEY",),
    }.get(name, ())
    for candidate in candidates:
        if os.environ.get(candidate):
            return candidate
    return None


def provider_profiles_with_sources(
    start: Optional[Path] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    """Return merged named profiles and the tier that last changed each name."""
    merged: Dict[str, Dict[str, Any]] = {}
    sources: Dict[str, str] = {}
    for label, _path, data in settings_chain(start):
        table = data.get("provider_profiles")
        if not isinstance(table, dict):
            continue
        for name, profile in table.items():
            if not isinstance(name, str) or not _PROFILE_NAME_PAT.fullmatch(name):
                continue
            if not isinstance(profile, dict):
                continue
            clean = {
                str(key): value
                for key, value in profile.items()
                if key in _PROFILE_FIELDS and value is not None
            }
            merged.setdefault(name, {}).update(clean)
            sources[name] = label
    return merged, sources


def provider_profiles(start: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Return all valid named provider profiles merged across settings tiers."""
    profiles, _sources = provider_profiles_with_sources(start)
    return profiles


def _selected_profile(
    flags: Optional[Dict[str, Any]] = None,
    effective: Optional[Dict[str, Any]] = None,
    start: Optional[Path] = None,
) -> Tuple[str, Dict[str, Any], str]:
    """Return ``(name, values, source)`` for the active provider profile."""
    raw = flags or {}
    inline = raw.get("provider_profile")
    if isinstance(inline, dict):
        return "<inline>", dict(inline), "profile:inline"
    name = raw.get("profile") or raw.get("provider_profile")
    source = "flag"
    if not isinstance(name, str) or not name.strip():
        name = os.environ.get("NEO_PROVIDER_PROFILE", "").strip()
        source = "env:NEO_PROVIDER_PROFILE"
    if not name and isinstance(effective, dict):
        candidate = effective.get("provider_profile")
        if isinstance(candidate, str):
            name = candidate.strip()
            source = "settings"
    profiles, profile_sources = provider_profiles_with_sources(start)
    if name and name in profiles:
        return (
            name,
            dict(profiles[name]),
            f"profile:{name}@{profile_sources.get(name, 'settings')}",
        )
    return str(name or ""), {}, source if name else ""


def active_provider_profile(
    flags: Optional[Dict[str, Any]] = None,
    effective: Optional[Dict[str, Any]] = None,
    start: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return non-secret metadata for the selected provider profile."""
    base = effective if isinstance(effective, dict) else effective_settings(start)
    name, _values, source = _selected_profile(flags, base, start)
    if name and source == "settings":
        source = value_source("provider_profile", base, start=start, flags=flags)
    return {
        "name": name,
        "source": source,
        "available": sorted(provider_profiles(start)),
    }


def _apply_profile_layers(
    base: Dict[str, Any],
    flags: Optional[Dict[str, Any]] = None,
    start: Optional[Path] = None,
) -> Dict[str, Any]:
    out = dict(base)
    _name, profile, _source = _selected_profile(flags, out, start)
    out.update(profile)
    inline = (flags or {}).get("provider_profile")
    if isinstance(inline, dict):
        out.update(inline)
    out.update(env_overrides())
    out.update({k: v for k, v in (flags or {}).items() if v is not None and v != ""})
    return out


def effective_settings(start: Optional[Path] = None) -> Dict[str, Any]:
    """File chain + active profile + env vars in precedence order."""
    out = _apply_profile_layers(merged_settings(start), start=start)
    env = env_overrides()
    if os.environ.get("NEO_BASE_URL"):
        out["api_base"] = env["base_url"]
    elif os.environ.get("NEO_API_BASE"):
        out["api_base"] = env["api_base"]
    if not out.get("api_key"):
        env_name = _provider_env_name(
            out.get("provider"), out.get("api_base") or out.get("base_url")
        )
        if env_name and os.environ.get(env_name):
            out["api_key"] = os.environ[env_name]
    return out


def apply_config_defaults(
    config: Dict[str, Any],
    file_config: Optional[Dict[str, Any]] = None,
    start: Optional[Path] = None,
) -> Dict[str, Any]:
    """Merge the whole settings chain UNDER an existing config dict.

    Precedence by construction: explicit (`config` — CLI flags, session
    state) > env (NEO_MODEL/NEO_BASE_URL/NEO_API_KEY/...) > files.
    ``start`` selects the repository chain for callers operating on a
    repository other than the process CWD. ``file_config`` may be a
    pre-loaded chain dict (the interactive session loads once at
    startup); None loads fresh. Returns a NEW dict.
    """
    base = file_config if file_config is not None else merged_settings(start)
    explicit = dict(config or {})
    out = _apply_profile_layers(base, flags=explicit, start=start)
    env = env_overrides()
    if "base_url" in explicit:
        out["api_base"] = explicit["base_url"]
    elif "api_base" in explicit:
        out["api_base"] = explicit["api_base"]
    elif os.environ.get("NEO_BASE_URL"):
        out["api_base"] = env["base_url"]
    elif os.environ.get("NEO_API_BASE"):
        out["api_base"] = env["api_base"]
    if not out.get("api_key"):
        env_name = _provider_env_name(
            out.get("provider"), out.get("api_base") or out.get("base_url")
        )
        if env_name and os.environ.get(env_name):
            out["api_key"] = os.environ[env_name]
    return out


def _env_source_for(key: str) -> Optional[str]:
    """Return the environment variable that supplies ``key``, if any."""
    for var, mapped in _ENV_KEYS.items():
        if mapped == key and os.environ.get(var):
            return var
    if key == "api_base" and os.environ.get("NEO_BASE_URL"):
        return "NEO_BASE_URL"
    if key == "base_url" and os.environ.get("NEO_API_BASE"):
        return "NEO_API_BASE"
    return None


def value_source(
    key: str,
    effective: Optional[Dict[str, Any]] = None,
    start: Optional[Path] = None,
    flags: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the highest-precedence source label for one setting.

    The returned labels are stable and safe to display: ``flag``,
    ``env:<variable>``, ``project-local``, ``project``, ``global``,
    ``legacy``, or ``default``. The function does not expose values and
    tolerates missing/broken files.
    """
    aliases = {
        "api_base": ("base_url",),
        "base_url": ("api_base",),
        "display_label": ("label",),
        "label": ("display_label",),
    }
    keys = (key, *aliases.get(key, ()))
    clean_flags = {k: v for k, v in (flags or {}).items() if v is not None and v != ""}
    for candidate in keys:
        if candidate in clean_flags:
            return "flag"
    for candidate in keys:
        env_var = _env_source_for(candidate)
        if env_var:
            return f"env:{env_var}"
    profile_name, profile_values, profile_source = _selected_profile(
        clean_flags, effective, start
    )
    if key == "api_key":
        provider = (
            clean_flags.get("provider")
            or (effective or {}).get("provider")
            or profile_values.get("provider")
        )
        base = (
            clean_flags.get("api_base")
            or clean_flags.get("base_url")
            or (effective or {}).get("api_base")
            or (effective or {}).get("base_url")
            or profile_values.get("api_base")
            or profile_values.get("base_url")
        )
        env_name = _provider_env_name(provider, base)
        if env_name and os.environ.get(env_name):
            return f"env:{env_name}"
    for candidate in keys:
        if candidate in profile_values:
            return profile_source or (
                f"profile:{profile_name}" if profile_name else "profile"
            )
    pd = project_settings_dir(start)
    if pd is not None:
        local_data = load_neo_config(pd / "settings.local.toml")
        project_data = load_neo_config(pd / "settings.toml")
        for candidate in keys:
            if candidate in local_data:
                return "project-local"
        for candidate in keys:
            if candidate in project_data:
                return "project"
    gp = global_settings_path()
    global_data = load_neo_config(gp) if gp.is_file() else {}
    for candidate in keys:
        if candidate in global_data:
            return "global"
    lp = legacy_settings_path()
    legacy_data = load_neo_config(lp) if lp.is_file() else {}
    for candidate in keys:
        if candidate in legacy_data:
            return "legacy"
    if effective is not None and key in effective:
        return "default"
    return "default"


def source_tier(
    key: str,
    effective: Optional[Dict[str, Any]] = None,
    start: Optional[Path] = None,
    flags: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the coarse precedence tier for one setting.

    ``env:<variable>`` is collapsed to ``env``; all other labels retain
    their useful tier name. This is the value intended for compact
    status/JSON surfaces.
    """
    label = value_source(key, effective=effective, start=start, flags=flags)
    if label.startswith("env:"):
        return "env"
    if label == "project-local":
        return "local"
    if label.startswith("profile:"):
        return "profile"
    return label


def effective_settings_with_sources(
    start: Optional[Path] = None,
    flags: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return effective values and per-key source metadata.

    The ``values`` mapping is suitable for runtime configuration and may
    contain an API key for local callers. The ``sources`` and
    ``source_tiers`` mappings contain no secret values and are safe to
    render or serialize.
    """
    clean_flags = {k: v for k, v in (flags or {}).items() if v is not None and v != ""}
    values = apply_config_defaults(clean_flags, start=start)
    keys = set(values) | set(clean_flags)
    sources = {
        key: value_source(key, values, start=start, flags=clean_flags) for key in keys
    }
    tiers = {
        key: source_tier(key, values, start=start, flags=clean_flags) for key in keys
    }
    return {"values": values, "sources": sources, "source_tiers": tiers}


def redact_url(value: Any) -> str:
    """Return an endpoint safe for logs and status output."""
    text = str(value or "")
    try:
        parts = urlsplit(text)
        if not parts.scheme or not parts.netloc:
            return redact_text(text)
        host = parts.hostname or ""
        try:
            port = parts.port
        except ValueError:
            return f"{parts.scheme}://[REDACTED]"
        if port:
            host += f":{port}"
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            query.append((key, "[REDACTED]" if _looks_secret_key(key) else item))
        fragment = "[REDACTED]" if parts.fragment else ""
        return urlunsplit((parts.scheme, host, parts.path, urlencode(query), fragment))
    except Exception:
        return "[REDACTED-URL]"


def _looks_secret_key(key: str) -> bool:
    """True when a query/header name conventionally carries a secret."""
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in {"key", "apikey", "xapikey"} or bool(
        re.search(
            r"(?i)(api[-_]?key|access[-_]?token|auth(?:orization)?|token|secret|password|passwd)",
            str(key),
        )
    )


def _redact_urls(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        suffix = ""
        while token and token[-1] in ".,;:)":
            suffix = token[-1] + suffix
            token = token[:-1]
        return redact_url(token) + suffix

    return re.sub(r"(?i)https?://[^\s<>\"']+", replace, text)


def redact_text(value: Any, secrets: Optional[List[str]] = None) -> str:
    """Redact common credential shapes from text without raising."""
    text = str(value or "")
    for secret in secrets or []:
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    text = _redact_urls(text)
    text = re.sub(
        r"(?i)([\"'](?:api[-_]?key|access[-_]?token|auth(?:orization)?|token|secret|password|passwd|key)[\"']\s*:\s*[\"'])([^\"']+)([\"'])",
        lambda m: f"{m.group(1)}***{m.group(3)}",
        text,
    )
    text = re.sub(
        r"(?i)(\b(?:api[-_]?key|access[-_]?token|auth(?:orization)?|token|secret|password|passwd|key)\b\s*=\s*)([^\s,;]+)",
        lambda m: f"{m.group(1)}***",
        text,
    )
    text = re.sub(
        r"(?i)(--(?:api[-_]?key|access[-_]?token|auth(?:orization)?|token|secret|password|passwd|key)(?:\s+|=))([^\s,;]+)",
        lambda m: f"{m.group(1)}***",
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    return text[:1000]


def _public_profile(profile: Dict[str, Any]) -> Dict[str, str]:
    return {str(key): public_value(str(key), value) for key, value in profile.items()}


def public_provider_profiles(
    start: Optional[Path] = None,
) -> Dict[str, Dict[str, str]]:
    """Return named provider profiles with keys and endpoint secrets masked."""
    return {
        name: _public_profile(profile)
        for name, profile in provider_profiles(start).items()
    }


def public_value(key: str, value: Any) -> Any:
    """Render a settings value for human or JSON status output."""
    if key == "api_key":
        return mask_secret(value)
    if key in ("api_base", "base_url"):
        return redact_url(value)
    if key == "provider_profiles" and isinstance(value, dict):
        return {
            str(name): _public_profile(profile)
            if isinstance(profile, dict)
            else "<structured>"
            for name, profile in value.items()
        }
    if key == "provider_profile" and isinstance(value, dict):
        return _public_profile(value)
    return str(value)


def resolve_provider_config(
    flags: Optional[Dict[str, Any]] = None,
    start: Optional[Path] = None,
) -> Dict[str, Any]:
    """Resolve provider fields and source metadata across every tier.

    Explicit flags win over environment values, which win over the active
    named profile and flat settings. The result contains the API key for
    runtime use and must be passed through :func:`public_provider_config`
    before display or serialization.
    """
    raw_flags = {k: v for k, v in (flags or {}).items() if v is not None and v != ""}
    resolved = apply_config_defaults(raw_flags, start=start)
    normalized = normalize_runtime_keys(resolved)
    sources = effective_settings_with_sources(start=start, flags=raw_flags)
    source_values = dict(sources["source_tiers"])
    source_labels = dict(sources["sources"])
    if normalized.get("api_base") and "base_url" not in source_values:
        source_values["base_url"] = source_values.get("api_base", "default")
        source_labels["base_url"] = source_labels.get("api_base", "default")
    profile_name, _profile_values, profile_source = _selected_profile(
        raw_flags, resolved, start
    )
    label = normalized.get("display_label") or normalized.get("label") or ""
    health = normalized.get("health_check", True)
    if isinstance(health, str):
        health = health.strip().lower() not in ("0", "false", "no", "off")
    return {
        "provider": normalized.get("provider"),
        "model": normalized.get("model"),
        "base_url": normalized.get("base_url") or normalized.get("api_base"),
        "api_base": normalized.get("api_base"),
        "api_key": normalized.get("api_key"),
        "display_label": label,
        "label": label,
        "health_check": bool(health),
        "profile": profile_name,
        "profile_source": profile_source,
        "source_tier": source_values.get("model", "default"),
        "source_tiers": source_values,
        "sources": source_labels,
    }


def public_provider_config(
    flags: Optional[Dict[str, Any]] = None,
    start: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return provider metadata with the key and endpoint credentials masked."""
    resolved = resolve_provider_config(flags=flags, start=start)
    resolved["api_key"] = (
        mask_secret(resolved.get("api_key", "")) if resolved.get("api_key") else ""
    )
    if resolved.get("api_base"):
        resolved["api_base"] = redact_url(resolved["api_base"])
    if resolved.get("base_url"):
        resolved["base_url"] = redact_url(resolved["base_url"])
    return resolved


def sessions_log_root(file_config: Optional[Dict[str, Any]] = None) -> Path:
    """Log root for session persistence: config `log_root` wins, else
    ./logs relative to CWD (the harness default)."""
    fc = file_config if file_config is not None else merged_settings()
    lr = fc.get("log_root")
    return Path(lr).expanduser() if isinstance(lr, str) and lr.strip() else Path("logs")


# ---------------------------------------------------------------------------
# Runtime-key normalization (Task B — custom router / base URL support)
# ---------------------------------------------------------------------------


def normalize_runtime_keys(config: Dict[str, Any]) -> Dict[str, Any]:
    """Translate settings-level keys to runtime Task.config keys.

    ``base_url`` maps onto ``api_base``. Named OpenAI-compatible routers
    are normalized to the ``openai`` provider only when an endpoint is
    present, so callers can use readable names without changing the
    public runtime contract. Explicit non-router providers remain
    untouched.
    """
    out = dict(config)
    base = out.pop("base_url", None)
    if base and not out.get("api_base"):
        out["api_base"] = base
    provider = str(out.get("provider") or "").strip().lower()
    if out.get("api_base") and (
        provider in {"agentrouter", "openrouter", "tokenrouter"} or not provider
    ):
        out["provider"] = "openai"
    return out


# ---------------------------------------------------------------------------
# Writing (neo config set / unset / init-project / first-run)
# ---------------------------------------------------------------------------


def _toml_scalar(value: Any) -> str:
    """Render a scalar as TOML. JSON string escaping is valid TOML for
    basic strings; raises ValueError for non-scalar/non-finite values."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite floats cannot be written as TOML")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise ValueError(f"cannot serialize {type(value).__name__} as a TOML scalar")


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def _dump_toml(data: Dict[str, Any], _path: str = "") -> str:
    """Serialize a (nested) settings dict back to TOML. Scalars first,
    then [table] sections — tomllib round-trips the result. Structured
    values the writer can't express raise ValueError (caller reports a
    clean error instead of corrupting the file)."""
    lines: List[str] = []
    for key, value in data.items():
        k = _toml_key(key)
        if isinstance(value, dict):
            if not value:
                continue  # empty tables have no TOML representation
            header = f"[{_path}{k}]"
            lines.append("")
            lines.append(header)
            lines.append(_dump_toml(value, _path + k + "."))
        elif isinstance(value, list):
            try:
                rendered = ", ".join(_toml_scalar(x) for x in value)
            except ValueError as exc:
                raise ValueError(f"cannot serialize list {key!r}: {exc}") from exc
            lines.append(f"{k} = [{rendered}]")
        else:
            lines.append(f"{k} = {_toml_scalar(value)}")
    return "\n".join(lines).lstrip("\n")


def _atomic_write_text(p: Path, text: str) -> None:
    """Write text atomically (unique tmp + fsync + replace).

    A crash mid-write must never destroy an existing settings file, and two
    concurrent writers must never interleave. Three properties, each of which
    was a real gap before:

    - the temp file name is UNIQUE (pid + a counter), so two processes
      writing the same target cannot share a ``.tmp`` and clobber each
      other's payload;
    - the payload is fsync'd before the replace, so a power loss cannot leave
      a replaced-but-empty file;
    - the final ``os.replace`` is retried briefly on ``PermissionError``.
      On Windows a concurrent READER holding the file without
      ``FILE_SHARE_DELETE`` makes the replace fail transiently, which is the
      same race ``runtime.fsutil`` already handles; a persistent denial
      still raises after the bounded retry.
    """
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{_next_tmp_id()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:  # pragma: no cover - platform dependent
                pass
        _replace_with_retry(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
    _secure_settings_file(p)


def _replace_with_retry(source: Path, destination: Path, attempts: int = 5) -> None:
    """``os.replace`` with a bounded retry for the Windows sharing race."""
    last: Optional[OSError] = None
    for attempt in range(max(1, int(attempts))):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.05 * (attempt + 1))
    if last is not None:
        raise last


_TMP_COUNTER = itertools.count()
_TMP_LOCK = threading.Lock()


def _next_tmp_id() -> int:
    """Return a process-unique temp-file counter."""
    with _TMP_LOCK:
        return next(_TMP_COUNTER)


#: Re-entrancy depth per thread, so a public mutator that calls another
#: mutator on the SAME path does not deadlock on its own lock.
_LOCK_DEPTH = threading.local()


class SettingsLockTimeout(TimeoutError):
    """Raised when a settings lock could not be acquired within its budget.

    A typed failure on purpose: silently proceeding without the lock is how a
    config file gets corrupted, and a caller deserves to be able to catch
    this and report "another process is writing settings" rather than
    "settings are mysteriously empty".
    """


@contextlib.contextmanager
def settings_lock(
    p: Path, timeout_s: float = 10.0, stale_s: float = 60.0
) -> "contextlib.AbstractContextManager[None]":
    """Hold a cross-process exclusive lock for one settings file.

    The lock is a sibling ``<name>.lock`` file created with ``O_EXCL``, which
    is atomic on every filesystem Neo supports. A lock whose owner died is
    taken over after ``stale_s`` — otherwise a crash would wedge the config
    permanently, which is a worse failure than the race it prevents.

    Re-entrant per thread: nesting ``settings_lock`` on the same path from one
    thread yields the inner critical section instead of deadlocking.
    """
    p = Path(p)
    depth = getattr(_LOCK_DEPTH, "depth", None)
    if depth is not None and depth.get("path") == os.path.normcase(str(p)):
        depth["count"] += 1
        try:
            yield
        finally:
            depth["count"] -= 1
        return
    lock_path = p.with_name(p.name + ".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    handle: Optional[int] = None
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while handle is None:
        try:
            handle = os.open(
                str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            age = 0.0
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > max(1.0, float(stale_s)):
                try:
                    lock_path.unlink()
                except OSError:  # pragma: no cover - lost the race, retry
                    pass
                continue
            if time.monotonic() >= deadline:
                raise SettingsLockTimeout(
                    f"another process is writing {p} (lock {lock_path}); "
                    "retry after it finishes"
                ) from None
            time.sleep(0.01)
    if handle is not None:
        try:
            os.write(handle, f"{os.getpid()}\n".encode("ascii", "replace"))
        except OSError:  # pragma: no cover - the lock file is advisory
            pass
    _LOCK_DEPTH.depth = {"path": os.path.normcase(str(p)), "count": 1}
    try:
        yield
    finally:
        # The attribute is REMOVED, not left at count 0: a leftover holder
        # would make the next top-level acquisition on this thread look
        # re-entrant and skip the real lock entirely.
        _LOCK_DEPTH.depth = None
        try:
            os.close(handle)
        except OSError:  # pragma: no cover
            pass
        try:
            lock_path.unlink()
        except OSError:  # pragma: no cover - best effort
            pass


def _as_int(value: Any, default: int) -> int:
    """Coerce a value to int, falling back to ``default`` on anything else."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_modify_write(
    p: Path,
    mutate: Callable[[Dict[str, Any], bool], Optional[str]],
    *,
    timeout_s: float = 10.0,
) -> Optional[str]:
    """Run a locked read-modify-write cycle and return what it changed.

    ``mutate`` receives ``(parsed_settings, file_existed)`` — both sampled
    INSIDE the lock — and returns the text to write, or ``None`` for "no
    change". The read, the decision, and the write all happen inside ONE
    lock, which is the property that makes concurrent ``neo login`` /
    ``neo config set`` / an onboarding wizard from three terminals safe.

    The existence flag matters and is easy to get wrong: a caller that
    samples "did this file exist?" before taking the lock and then branches
    on that stale answer will write its own value as if the file were still
    absent, deleting everything another writer added in between. That was a
    real lost-update bug in this module, found by the concurrent-write test.

    The whole cycle is refused on a file that is not valid TOML rather than
    overwriting what a human wrote by hand.

    A transient write failure (the Windows sharing violation: another process
    holding the file without ``FILE_SHARE_DELETE`` makes ``os.replace`` fail)
    re-runs the WHOLE cycle — re-acquire, re-read, re-decide, re-write — a
    bounded number of times. Re-running the cycle rather than just the write
    matters: a retry that only re-attempted the replace would be writing a
    decision made from a now-stale read, which is the lost-update bug in a
    different costume.
    """
    attempts = max(1, _as_int(os.environ.get("NEO_SETTINGS_WRITE_ATTEMPTS"), 4))
    last: Optional[OSError] = None
    for attempt in range(attempts):
        try:
            with settings_lock(p, timeout_s=timeout_s):
                existed = p.is_file()
                data = _read_settings(p) if existed else {}
                if data is None:
                    raise ValueError(
                        f"{p} is not valid TOML — fix it by hand before writing "
                        "(refusing to overwrite)"
                    )
                text = mutate(data, existed)
                if text is None:
                    return None
                _atomic_write_text(p, text)
                return text
        except OSError as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(0.05 * (attempt + 1))
    if last is not None:
        raise last
    return None  # pragma: no cover - the loop always returns or raises


def _append_text(p: Path, block: str, data: Optional[Dict[str, Any]]) -> str:
    """Return the file text with ``block`` appended, preserving what is there.

    Appending is the behavior that preserves hand-written comments, and it
    must be atomic for the same reason a rewrite is: a reader that opens the
    file between two appends must never see a half-written key. The caller
    holds the lock (through :func:`_read_modify_write`).
    """
    existing = ""
    if p.is_file():
        existing = p.read_text(encoding="utf-8-sig")
        if existing and not existing.endswith("\n"):
            existing += "\n"
    return existing + block


def _secure_settings_file(p: Path) -> None:
    """Best-effort chmod 600 on a settings file (POSIX only — secrets
    may live here). Never raises: a best-effort hardening must not
    break a save on odd filesystems."""
    try:
        if os.name != "nt":
            os.chmod(p, 0o600)
    except OSError:
        pass


def coerce_value(key: str, text: str) -> Any:
    """Coerce a CLI string to the value type the key expects: booleans,
    ints, floats — but known string keys (model names like "3.5"!) always
    stay strings."""
    t = text.strip()
    if key in _STRING_KEYS:
        return t
    if t.lower() in ("true", "false"):
        return t.lower() == "true"
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+([eE][+-]?\d+)?", t):
        return float(t)
    return t


def mask_secret(value: Any) -> str:
    """Mask a secret for display: first 3 + last 4 chars when long
    enough, else fully hidden."""
    s = str(value)
    if len(s) <= 8:
        return "***"
    return f"{s[:3]}...{s[-4:]} (set)"


def set_tier_key(
    tier: str, key: str, value: Any, start: Optional[Path] = None
) -> Tuple[Path, bool]:
    """Write one key into a tier's settings file.

    Returns (path, created_file). Append-in-place when the key is new to
    an existing file (preserves the user's hand-written comments); a
    full (comment-less) rewrite only when the key already exists.

    The read, the decision, and the write happen inside ONE cross-process
    lock, so concurrent writers from several terminals cannot lose an update
    or produce a torn file. Raises ValueError for structured keys,
    unwritable values, a project-tier api_key (secrets never belong in the
    committable project file — use the global or local tier), a
    broken existing file (never overwrite a file we can't parse), or a lock
    that another process is holding past its budget.
    """
    if key in _STRUCTURED_KEYS:
        raise ValueError(
            f"{key} is a structured value — edit the settings file by hand"
        )
    if tier in ("project", "project-tier") and key == "api_key":
        raise ValueError(
            "refusing to store api_key in the committable project "
            "settings.toml (it would be committed) — use the default "
            "global tier or --tier local instead"
        )
    p = tier_path(tier, start)
    if p.is_symlink():
        raise ValueError(f"refusing to write settings through symlink: {p}")
    rendered = f"{_toml_key(key)} = {_toml_scalar(value)}\n"
    # ``created`` is set INSIDE the lock: sampling it before the lock is the
    # stale-read bug this function's locking exists to prevent.
    state = {"created": False}

    def _mutate(data: Dict[str, Any], existed: bool) -> Optional[str]:
        state["created"] = not existed
        if existed:
            if key in data:
                data[key] = value
                return _dump_toml(data) + "\n"
            # new key: append, preserving whatever the file already contains
            return _append_text(p, rendered, data)
        starter = _STARTER_GLOBAL if tier == "global" else _STARTER_PROJECT
        return starter + rendered

    p.parent.mkdir(parents=True, exist_ok=True)
    _read_modify_write(p, _mutate)
    return p, state["created"]


def _validate_profile_name(name: str) -> str:
    if not isinstance(name, str) or not _PROFILE_NAME_PAT.fullmatch(name):
        raise ValueError(
            "profile names must start with a letter or digit and contain only "
            "letters, digits, dots, dashes, or underscores"
        )
    return name


def set_provider_profile(
    name: str,
    values: Dict[str, Any],
    tier: str = "global",
    start: Optional[Path] = None,
) -> Path:
    """Create or update a named provider profile in one settings tier.

    Project-tier API keys are refused. Existing files are parsed before
    rewriting, and a new profile table is appended without discarding
    hand-written comments.
    """
    _validate_profile_name(name)
    if tier not in ("global", "project", "local"):
        raise ValueError(f"unknown profile tier: {tier!r}")
    if not isinstance(values, dict) or not values:
        raise ValueError("provider profile values must be a non-empty object")
    unknown = set(values) - _PROFILE_FIELDS
    if unknown:
        raise ValueError(
            "unsupported provider profile field(s): " + ", ".join(sorted(unknown))
        )
    if tier == "project" and values.get("api_key"):
        raise ValueError("refusing to store api_key in a committable project profile")
    if (
        "base_url" in values
        and "api_base" in values
        and values["base_url"] != values["api_base"]
    ):
        raise ValueError("profile base_url and api_base disagree")
    clean = {str(key): value for key, value in values.items() if value is not None}
    if "base_url" in clean:
        clean["api_base"] = clean.pop("base_url")
    elif "api_base" in clean:
        clean["base_url"] = clean["api_base"]
    p = tier_path(tier, start)
    if p.is_symlink():
        raise ValueError(f"refusing to write settings through symlink: {p}")

    def _mutate(data: Dict[str, Any], existed: bool) -> Optional[str]:
        table = data.get("provider_profiles")
        if table is not None and not isinstance(table, dict):
            raise ValueError(f"{p}: provider_profiles must be a table")
        table = dict(table or {})
        profile = (
            dict(table.get(name) or {}) if isinstance(table.get(name), dict) else {}
        )
        profile.update(clean)
        table[name] = profile
        if not existed:
            starter = _STARTER_GLOBAL if tier == "global" else _STARTER_PROJECT
            return starter + _dump_toml({"provider_profiles": {name: profile}}) + "\n"
        if "provider_profiles" not in data:
            # First profile in a hand-maintained file: append the table block
            # instead of rewriting, so the comments survive.
            block = _dump_toml({"provider_profiles": {name: profile}})
            return _append_text(p, "\n" + block + "\n", data)
        data["provider_profiles"] = table
        return _dump_toml(data) + "\n"

    p.parent.mkdir(parents=True, exist_ok=True)
    _read_modify_write(p, _mutate)
    return p


def remove_provider_profile(
    name: str, tier: str = "global", start: Optional[Path] = None
) -> Tuple[str, Path]:
    """Remove a named profile from one tier; return ``(name, path)``.

    The existence checks and the rewrite share one lock, so a concurrent
    writer cannot make the table disappear between the check and the write.
    """
    _validate_profile_name(name)
    p = tier_path(tier, start)
    if not p.is_file():
        raise ValueError(f"no provider profile named {name!r} in {tier} settings")

    def _mutate(data: Dict[str, Any], existed: bool) -> Optional[str]:
        table = data.get("provider_profiles")
        if not isinstance(table, dict) or name not in table:
            raise ValueError(f"no provider profile named {name!r} in {tier} settings")
        table = dict(table)
        del table[name]
        if table:
            data["provider_profiles"] = table
        else:
            data.pop("provider_profiles", None)
        return _dump_toml(data) + "\n"

    _read_modify_write(p, _mutate)
    return name, p


def select_provider_profile(
    name: str, tier: str = "global", start: Optional[Path] = None
) -> Tuple[str, Path]:
    """Select a named profile at one precedence tier."""
    _validate_profile_name(name)
    profiles = provider_profiles(start)
    if name not in profiles:
        raise ValueError(f"no provider profile named {name!r}")
    path, _created = set_tier_key(tier, "provider_profile", name, start=start)
    return name, path


def clear_provider_profile(
    tier: str = "global", start: Optional[Path] = None
) -> Tuple[str, Path]:
    """Clear the selected profile name at one precedence tier."""
    return unset_tier_key(tier, "provider_profile", start=start)


def _unset_key_from_path(p: Path, key: str) -> Tuple[str, Path]:
    """Remove one key from a settings file under the file's lock.

    Returns ("removed", path) or ("absent", path). A broken file raises
    ValueError (never overwrite what we can't parse), and a lock held past
    its budget raises :class:`SettingsLockTimeout` — a caller must be able to
    tell "the key was not there" from "another process is writing".
    """
    if not p.is_file():
        return "absent", p

    def _mutate(data: Dict[str, Any], existed: bool) -> Optional[str]:
        if key not in data:
            return None
        del data[key]
        return _dump_toml(data) + "\n"

    written = _read_modify_write(p, _mutate)
    return ("removed" if written is not None else "absent"), p


def unset_tier_key(
    tier: str, key: str, start: Optional[Path] = None
) -> Tuple[str, Path]:
    """Remove a key from a tier's file. Returns ("removed", path) or
    ("absent", path) when the key/file wasn't there. Broken files raise
    ValueError (never overwrite what we can't parse)."""
    return _unset_key_from_path(tier_path(tier, start), key)


def _persisted_api_key_paths(
    start: Optional[Path] = None,
) -> List[Tuple[str, Path]]:
    candidates = [
        ("global", global_settings_path()),
        ("project-local", tier_path("local", start)),
        ("legacy", legacy_settings_path()),
        ("project", tier_path("project", start)),
    ]
    out: List[Tuple[str, Path]] = []
    seen: set[str] = set()
    for label, path in candidates:
        try:
            identity = os.path.normcase(str(path.resolve()))
        except (OSError, RuntimeError, ValueError):
            identity = os.path.normcase(str(path))
        if identity in seen:
            continue
        seen.add(identity)
        out.append((label, path))
    return out


def remove_persisted_api_keys(
    start: Optional[Path] = None,
) -> Dict[str, Any]:
    """Remove api_key from every supported persisted settings tier.

    Assumes the paths come from the configured project/global roots and
    returns tier labels, removed paths, absent tiers, and non-secret
    error strings. Environment variables are not modified.
    """
    removed: List[str] = []
    absent: List[str] = []
    removed_paths: List[str] = []
    errors: List[str] = []
    for label, path in _persisted_api_key_paths(start):
        try:
            outcome, _ = _unset_key_from_path(path, "api_key")
        except (OSError, ValueError) as exc:
            errors.append(f"{label}: {exc}")
            continue
        if outcome == "removed":
            removed.append(label)
            removed_paths.append(str(path))
        else:
            absent.append(label)
    return {
        "removed": removed,
        "removed_paths": removed_paths,
        "absent": absent,
        "errors": errors,
    }


def ensure_first_run() -> Tuple[bool, Path]:
    """First-run flow: create the global settings dir + starter file when
    missing. Returns (created, path). Never raises (best-effort — a
    read-only home must not crash the interactive session).

    The existence check and the write share the file lock, so two sessions
    starting at once cannot both decide to create the file and one of them
    lose the other's first-run notice.
    """
    gp = global_settings_path()
    if gp.is_file():
        return False, gp
    try:
        gp.parent.mkdir(parents=True, exist_ok=True)

        def _mutate(_data: Dict[str, Any], existed: bool) -> Optional[str]:
            if existed:
                return None
            return _STARTER_GLOBAL

        written = _read_modify_write(gp, _mutate)
        return (written is not None), gp
    except (OSError, SettingsLockTimeout) as exc:
        _warn(f"cannot create {gp}: {exc}")
        return False, gp


def ensure_project(root: Path) -> Tuple[bool, Path]:
    """`neo config init-project`: create <root>/.neo/settings.toml when
    missing (committable, secrets-free). Returns (created, path).

    Also scaffolds the rest of the project layout when absent (never
    overwriting): settings.local.toml (key-less starter, git-ignored),
    commands/ + skills/ with one working example each. See
    ensure_project_layout for the full created list."""
    info = ensure_project_layout(root)
    return info["settings_created"], info["path"]


def find_git_root(start: Optional[Path] = None) -> Optional[Path]:
    """Nearest ancestor of `start` (default: CWD) containing a `.git`
    entry (a dir for repos, a file for worktrees/submodules). None
    when not inside a git repo — the auto-scaffold gate."""
    d = Path(start) if start is not None else Path.cwd()
    for cand in (d, *d.parents):
        try:
            if (cand / ".git").exists():
                return cand
        except OSError:
            return None
    return None


def _scaffold_target(root: Path, relative: str) -> Path:
    """Resolve a scaffold path and refuse links or paths outside ``root``."""
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise OSError(f"scaffold path escapes repository: {relative}") from exc
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise OSError(f"refusing to write through symlink: {current}")
    return candidate


def _project_dir_name(root: Path) -> str:
    """The directory name the product reads AND writes for `root`.

    Resolved through :func:`project_settings_dir` — the one reader — so the
    scaffold can never create a directory the reader then PREFERS over the
    one it was already reading. Measured on an upgrading user with a
    ``.vex/`` repository: a scaffold that hardcoded ``.neo`` created a
    second, empty-ish directory, the reader preferred it on the very next
    call, and the user's existing ``.vex/settings.toml`` was silently
    ignored. A scaffold that disagrees with the reader is a settings-loss
    bug, not a tidy-up.

    An explicit ``$NEO_PROJECT_DIR`` / ``$VEX_PROJECT_DIR`` names the
    settings directory itself, so its OWN basename is the answer. Reading
    the basename rather than assuming the current name matters: a user who
    exported ``NEO_PROJECT_DIR=/srv/myproj/.vex`` was getting a ``.neo/``
    scaffolded and a ``.neo/`` ignore entry for files that live under
    ``.vex/``, which is the same split-brain above wearing a different hat.

    Returns ``.neo`` when nothing names a directory, which is the new-user
    case and the only case in which this function chooses a name.
    """
    for var in ("NEO_PROJECT_DIR", "VEX_PROJECT_DIR"):
        env = os.environ.get(var)
        if not env or not env.strip():
            continue
        # A malformed value degrades to repo detection rather than answering
        # with a location that names no settings directory (see
        # project_settings_dir). "" and "." are the measured shapes.
        candidate = Path(env.strip()).expanduser()
        if candidate.name and candidate.name not in (".", ".."):
            return candidate.name
    try:
        resolved = project_settings_dir(root)
    except Exception:
        return ".neo"
    if resolved is not None:
        try:
            if resolved.parent == Path(root):
                return resolved.name
        except OSError:
            return ".neo"
    return ".neo"


def _gitignore_entries(dir_name: str) -> Tuple[str, str]:
    """The two personal-artifact ignore entries for `dir_name`.

    Derived rather than hardcoded so the ignore file always names the
    directory the personal files actually live in. A user whose repository
    still uses the previous name gets that name ignored, not `.neo/`.
    """
    return (
        f"{dir_name}/settings.local.toml",
        f"{dir_name}/connectors.local.toml",
    )


def ensure_project_layout(root: Path) -> Dict[str, Any]:
    """Create each missing project artifact without overwriting any file.

    The layout contains settings, a personal settings file, a command, a
    skill, and committable/personal connector examples. Every target is
    checked independently, so an existing command directory does not
    suppress a missing command file. Returns the settings path, whether
    the primary settings file was created, and exact relative paths
    created. Raises OSError for an unsafe or unwritable target; the
    automatic session hook catches that and keeps the session usable.

    The directory is the one the product already reads
    (:func:`_project_dir_name`), which is ``.neo`` for a new repository and
    the PREVIOUS name for one that still uses it. An upgrade therefore adds
    any missing file to the directory the user already has and creates no
    second one.
    """
    root = Path(root)
    name = _project_dir_name(root)
    d = root / name
    created: List[str] = []
    targets = (
        (f"{name}/settings.toml", _STARTER_PROJECT, True),
        (f"{name}/settings.local.toml", _STARTER_LOCAL, False),
        (f"{name}/commands/fix.md", _EXAMPLE_COMMAND_MD, False),
        (f"{name}/skills/code-review/SKILL.md", _EXAMPLE_SKILL_MD, False),
        (f"{name}/connectors.toml", _EXAMPLE_CONNECTORS_TOML, False),
        (f"{name}/connectors.local.toml", _EXAMPLE_CONNECTORS_LOCAL_TOML, False),
    )
    settings_created = False
    for relative, content, primary in targets:
        target = _scaffold_target(root, relative)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, content)
        created.append(relative.removeprefix(f"{name}/"))
        if primary:
            settings_created = True
    return {
        "path": d / "settings.toml",
        "settings_created": settings_created,
        "created": created,
        "dir_name": name,
    }


def maybe_scaffold_repo(start: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """First-`neo`-in-a-repo auto-scaffold: when `start`/CWD is inside
    a git repo, ensure its .neo/ layout exists (settings.toml +
    settings.local.toml + commands/ + skills/ examples) and its
    .gitignore covers the local file.

    Never overwrites, never scaffolds outside a git repo, never
    raises (best-effort — a read-only checkout must not crash the
    session). Returns the ensure_project_layout info dict (+
    "gitignore" outcome, "root") when inside a repo, None otherwise.

    $NEO_PROJECT_DIR (which points AT the .neo dir) overrides repo
    detection — its parent is scaffolded even without a .git entry
    (test isolation / portable layouts).
    """
    try:
        env = os.environ.get("NEO_PROJECT_DIR")
        if env and env.strip():
            root = Path(env.strip()).expanduser().parent
        else:
            root = find_git_root(start)
            if root is None:
                return None
        info = ensure_project_layout(root)
        try:
            info["gitignore"] = ensure_gitignore(root)
        except Exception:
            info["gitignore"] = None
        info["root"] = root
        return info
    except Exception as exc:
        _warn_once(f"scaffold|{start or Path.cwd()}", f"cannot scaffold .neo/: {exc}")
        return None


# ---------------------------------------------------------------------------
# .gitignore handling for .neo/settings.local.toml
# ---------------------------------------------------------------------------


def _gitignore_covers(text: str, entry: str) -> bool:
    """True when an existing .gitignore already ignores `entry` (exact
    line, whole-dir ignore, or a glob matching the path/basename)."""
    base = entry.rsplit("/", 1)[-1]
    for raw in text.splitlines():
        pat = raw.strip()
        if not pat or pat.startswith("#"):
            continue
        if pat == entry or pat == f"{entry.rsplit('/', 1)[0]}/":
            return True
        if pat.startswith("!"):  # negation: don't claim coverage
            continue
        if fnmatch.fnmatch(entry, pat) or fnmatch.fnmatch(base, pat):
            return True
    return False


def ensure_gitignore(project_root: Path) -> Optional[str]:
    """Ensure both Neo personal files are ignored in a repository.

    Returns ``present`` when every existing personal artifact is covered,
    ``appended`` when one or more entries were added, ``created`` when a
    new ignore file was made inside a git repository, or ``None`` when
    the repository cannot be updated.
    """
    root = Path(project_root)
    entries = _gitignore_entries(_project_dir_name(root))
    gi = root / ".gitignore"
    if gi.is_file():
        try:
            text = gi.read_text(encoding="utf-8")
        except OSError:
            return None
        missing = [entry for entry in entries if not _gitignore_covers(text, entry)]
        if not missing:
            return "present"
        if text and not text.endswith("\n"):
            text += "\n"
        text += "# Neo local settings (personal overrides — never commit)\n"
        text += "".join(entry + "\n" for entry in missing)
        try:
            _atomic_write_text(gi, text)
        except OSError:
            return None
        return "appended"
    if (root / ".git").exists():
        try:
            _atomic_write_text(
                gi,
                "# Neo local settings (personal overrides — never commit)\n"
                + "".join(entry + "\n" for entry in entries),
            )
        except OSError:
            return None
        return "created"
    return None


def local_is_ignored(project_root: Path) -> bool:
    """True when every existing Neo personal file is ignored.

    Checks the directory the product actually reads (see
    :func:`_project_dir_name`), so a repository still using the previous
    directory name is judged on that name's entries.
    """
    root = Path(project_root)
    gi = root / ".gitignore"
    if not gi.is_file():
        return False
    try:
        text = gi.read_text(encoding="utf-8")
    except OSError:
        return False
    settings_entry, connectors_entry = _gitignore_entries(_project_dir_name(root))
    required = []
    personal = root / _project_dir_name(root)
    if (personal / "settings.local.toml").exists():
        required.append(settings_entry)
    if (personal / "connectors.local.toml").exists():
        required.append(connectors_entry)
    return all(_gitignore_covers(text, entry) for entry in required)
