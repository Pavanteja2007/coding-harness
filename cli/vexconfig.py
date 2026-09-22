"""Vex user settings — TWO-TIER config (global + project), matching the
Claude Code pattern (two-tier config round, 2026-09-13).

Directories (created by Vex's own first-run flow and the `vex config`
command — never by installers):

    Global   Windows: %APPDATA%\\vex\\settings.toml
             POSIX:   ~/.config/vex/settings.toml (XDG_CONFIG_HOME honored)
             (the file named by $VEX_CONFIG always wins for the global tier)
    Project  <repo>/.vex/settings.toml          safe to commit (no secrets)
             <repo>/.vex/settings.local.toml    personal overrides, auto-
                                                 added to the repo's
                                                 .gitignore when Vex
                                                 creates it (Claude Code's
                                                 settings.local.json
                                                 pattern)
             <repo>/.vex/commands/<name>.md     project slash commands
             <repo>/.vex/skills/<name>/SKILL.md project skills
             (the whole project layout is scaffolded with examples on
             the first `vex` run inside a git repo — never overwritten,
             never outside a repo; `vex config init-project` is the
             explicit form)

The project tier is found by walking up from the CWD for a `.vex/`
directory ($VEX_PROJECT_DIR overrides — points AT the .vex dir).

Precedence, highest to lowest:

    1. explicit CLI flags / session state (the caller's own dict)
    2. environment: VEX_MODEL, VEX_PROVIDER, VEX_BASE_URL, VEX_API_BASE,
       VEX_API_KEY
    3. project .vex/settings.local.toml
    4. project .vex/settings.toml
    5. global settings.toml ($VEX_CONFIG or the platform path)
    6. legacy ~/.vex/config.toml — read ONLY when the new global file is
       missing (pre-two-tier installs; `vex config path` shows the
       migration hint)
    7. built-in defaults (applied by the harness's config merge, not here)

Schema: all keys optional; unknown keys pass through untouched (same
philosophy as harness.get_config — future/other-terminal knobs work
without this module changing). One nested `[vex]` table is accepted for
grouping. Keys this module itself interprets: model, provider,
base_url (alias of runtime's api_base), api_base, api_key,
budget_cap_usd, max_retries, plan_preview, log_verbosity, log_root.

TOML parse errors NEVER crash the CLI: a broken file is reported once on
stderr and ignored (defaults apply) — a settings file is a convenience,
not a load-bearing input.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Keys this module itself interprets (anything else passes through to the
# harness/router config merge untouched).
_VEX_KEYS = {
    "model",
    "provider",
    "base_url",
    "api_base",
    "api_key",
    "budget_cap_usd",
    "max_retries",
    "plan_preview",
    "log_verbosity",
}

# Keys that must stay strings even when they look numeric (model "3.5").
_STRING_KEYS = {
    "model",
    "provider",
    "base_url",
    "api_base",
    "api_key",
    "log_verbosity",
}

# Structured (dict-valued) keys the CLI's `config set` refuses to write —
# editing those by hand in the file is the supported path.
_STRUCTURED_KEYS = {"model_tiers", "difficulty_llm"}

# Env var -> settings key (tier 2 of the precedence chain).
_ENV_KEYS = {
    "VEX_MODEL": "model",
    "VEX_PROVIDER": "provider",
    "VEX_BASE_URL": "base_url",
    "VEX_API_BASE": "api_base",
    "VEX_API_KEY": "api_key",
}

_LOCAL_GITIGNORE_ENTRY = ".vex/settings.local.toml"

_STARTER_HEADER = (
    "# Vex settings — `vex config list` shows the effective values.\n"
    "# Precedence (highest first): CLI flags > env (VEX_MODEL, VEX_BASE_URL,\n"
    "# VEX_API_KEY, VEX_PROVIDER, ...) > .vex/settings.local.toml >\n"
    "# .vex/settings.toml > this file.\n"
)
_STARTER_GLOBAL = (
    _STARTER_HEADER
    + "#\n"
    + "# Keys: model, provider, base_url, api_key, budget_cap_usd,\n"
    + "# max_retries, plan_preview, log_verbosity, log_root.\n"
    + "# Point Vex at any OpenAI-compatible router with\n"
    + "#   vex config set base_url https://my-router.example.com/v1\n"
    + "#   vex config set model <any model name the router serves>\n"
)
_STARTER_PROJECT = (
    _STARTER_HEADER
    + "#\n"
    + "# Safe to commit — keep secrets in settings.local.toml or the\n"
    + "# global file, never here.\n"
)
_STARTER_LOCAL = (
    "# Vex local settings — personal overrides for this repo only.\n"
    "# This file is git-ignored (never committed); put secrets here,\n"
    '# e.g. api_key = "..." — `vex config list` shows effective values.\n'
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


# ---------------------------------------------------------------------------
# Tier paths
# ---------------------------------------------------------------------------


def global_settings_path() -> Path:
    """The global settings file: $VEX_CONFIG wins, else the platform
    location (%APPDATA%\\vex on Windows, ~/.config/vex elsewhere)."""
    env = os.environ.get("VEX_CONFIG")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "vex" / "settings.toml"


def legacy_settings_path() -> Path:
    """The pre-two-tier single config file (~/.vex/config.toml). Read only
    as a fallback when the new global file is missing; $VEX_LEGACY_CONFIG
    overrides (test isolation)."""
    env = os.environ.get("VEX_LEGACY_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".vex" / "config.toml"


def project_settings_dir(start: Optional[Path] = None) -> Optional[Path]:
    """The project's .vex/ directory: $VEX_PROJECT_DIR wins (authoritative
    even when not yet on disk — `vex config set --tier project` creates
    it), else the nearest ancestor of the CWD (or `start`) that contains
    one. None when no project config exists on the chain."""
    env = os.environ.get("VEX_PROJECT_DIR")
    if env:
        return Path(env).expanduser()
    d = Path(start) if start is not None else Path.cwd()
    for cand in (d, *d.parents):
        if (cand / ".vex").is_dir():
            return cand / ".vex"
    return None


def project_settings_path(start: Optional[Path] = None) -> Optional[Path]:
    """<project>/.vex/settings.toml (None when no project dir found)."""
    d = project_settings_dir(start)
    return d / "settings.toml" if d else None


def local_settings_path(start: Optional[Path] = None) -> Optional[Path]:
    """<project>/.vex/settings.local.toml (None when no project dir)."""
    d = project_settings_dir(start)
    return d / "settings.local.toml" if d else None


def tier_path(tier: str, start: Optional[Path] = None) -> Path:
    """Resolve a tier name ("global" | "project" | "local") to its file.

    For the project tiers this is the WRITE view: when no .vex/ is found
    by walking up, the target is <CWD>/.vex/... (set creates it), rather
    than None.
    """
    if tier == "global":
        return global_settings_path()
    if tier in ("project", "local"):
        d = project_settings_dir(start)
        if d is None:
            base = Path(start) if start is not None else Path.cwd()
            d = base / ".vex"
        name = "settings.toml" if tier == "project" else "settings.local.toml"
        return d / name
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
    if key in ("plan_preview",):
        return isinstance(value, bool)
    return True


def _warn(msg: str) -> None:
    """One-shot stderr warning (plain print — ui import would be circular
    here for callers that import this module before ui)."""
    print(f"vex config: {msg}", file=sys.stderr)


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
    # one nested table is allowed for grouping: [vex] ...
    if "vex" in data and isinstance(data["vex"], dict):
        merged = dict(data["vex"])
        merged.update({k: v for k, v in data.items() if k != "vex"})
        data = merged

    for key, value in data.items():
        if key in _VEX_KEYS and not _type_ok(key, value):
            _warn_once(
                f"{p}|type:{key}",
                f"{p}: {key} has wrong type ({type(value).__name__}) — dropping it",
            )
            continue
        out[key] = value
    return out


def load_vex_config(path: Optional[Path] = None) -> Dict[str, Any]:
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
        tiers.append(("global", gp, load_vex_config(gp)))
    elif legacy_settings_path().is_file():
        lp = legacy_settings_path()
        tiers.append(("legacy", lp, load_vex_config(lp)))
    pd = project_settings_dir(start)
    if pd is not None:
        ps = pd / "settings.toml"
        ls = pd / "settings.local.toml"
        if ps.is_file():
            tiers.append(("project", ps, load_vex_config(ps)))
        if ls.is_file():
            tiers.append(("project-local", ls, load_vex_config(ls)))
    return tiers


def merged_settings(start: Optional[Path] = None) -> Dict[str, Any]:
    """All file tiers folded together (legacy < global < project < local).

    This is tiers 3-6 of the precedence chain WITHOUT env vars — env is
    applied by effective_settings()/apply_config_defaults so explicit
    caller values keep a single uniform place to win.
    """
    out: Dict[str, Any] = {}
    for _label, _p, data in settings_chain(start):
        out.update(data)
    return out


def env_overrides() -> Dict[str, Any]:
    """Settings from VEX_* environment variables (tier 2). Empty-string
    values are ignored (an exported-but-blank var must not blank a file)."""
    out: Dict[str, Any] = {}
    for var, key in _ENV_KEYS.items():
        val = os.environ.get(var)
        if val:  # non-empty only
            out[key] = val
    return out


def effective_settings(start: Optional[Path] = None) -> Dict[str, Any]:
    """File chain + env vars — the settings in effect when the user
    passed no explicit flags (`vex config list/get` render this)."""
    out = merged_settings(start)
    out.update(env_overrides())
    return out


def apply_config_defaults(
    config: Dict[str, Any],
    file_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge the whole settings chain UNDER an existing config dict.

    Precedence by construction: explicit (`config` — CLI flags, session
    state) > env (VEX_MODEL/VEX_BASE_URL/VEX_API_KEY/...) > files.
    `file_config` may be a pre-loaded chain dict (the interactive session
    loads once at startup); None loads fresh. Returns a NEW dict.
    """
    base = file_config if file_config is not None else merged_settings()
    out: Dict[str, Any] = dict(base)
    out.update(env_overrides())
    out.update(config or {})
    return out


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
    """Translate settings-level keys to the runtime's Task.config keys.

    base_url (the friendlier name users set) maps onto runtime's
    api_base; when an api_base is in play and no provider was chosen,
    provider defaults to "openai" — ANY OpenAI-compatible endpoint then
    works with ANY model name it serves (litellm dials {base_url}/chat/
    completions with that model). An explicit provider always wins.
    Returns a new dict; `base_url` is consumed (not left behind to
    confuse downstream config consumers).
    """
    out = dict(config)
    base = out.pop("base_url", None)
    if base and not out.get("api_base"):
        out["api_base"] = base
    if out.get("api_base") and not out.get("provider"):
        out["provider"] = "openai"
    return out


# ---------------------------------------------------------------------------
# Writing (vex config set / unset / init-project / first-run)
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
    """Write text atomically (tmp + os.replace) so a crash mid-write
    never destroys an existing settings file."""
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    _secure_settings_file(p)


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
    Raises ValueError for structured keys, unwritable values, a
    project-tier api_key (secrets never belong in the committable
    project file — use the global or local tier), or a
    broken existing file (never overwrite a file we can't parse)."""
    if key in _STRUCTURED_KEYS:
        raise ValueError(
            f"{key} is a structured value — edit the settings file by hand"
        )
    if tier == "project" and key == "api_key":
        raise ValueError(
            "refusing to store api_key in the committable project "
            "settings.toml (it would be committed) — use the default "
            "global tier or --tier local instead"
        )
    p = tier_path(tier, start)
    rendered = f"{_toml_key(key)} = {_toml_scalar(value)}\n"
    if p.is_file():
        data = _read_settings(p)
        if data is None:
            raise ValueError(
                f"{p} is not valid TOML — fix it by hand before using "
                "`vex config set` (refusing to overwrite)"
            )
        if key in data:
            data[key] = value
            _atomic_write_text(p, _dump_toml(data) + "\n")
            return p, False
        # new key: append, preserving whatever the file already contains
        text = p.read_text(encoding="utf-8-sig")
        if text and not text.endswith("\n"):
            text += "\n"
        p.write_text(text + rendered, encoding="utf-8")
        _secure_settings_file(p)
        return p, False
    p.parent.mkdir(parents=True, exist_ok=True)
    starter = _STARTER_GLOBAL if tier == "global" else _STARTER_PROJECT
    _atomic_write_text(p, starter + rendered)
    return p, True


def unset_tier_key(
    tier: str, key: str, start: Optional[Path] = None
) -> Tuple[str, Path]:
    """Remove a key from a tier's file. Returns ("removed", path) or
    ("absent", path) when the key/file wasn't there. Broken files raise
    ValueError (never overwrite what we can't parse)."""
    p = tier_path(tier, start)
    if not p.is_file():
        return "absent", p
    data = _read_settings(p)
    if data is None:
        raise ValueError(f"{p} is not valid TOML — refusing to rewrite it")
    if key not in data:
        return "absent", p
    del data[key]
    _atomic_write_text(p, _dump_toml(data) + "\n")
    return "removed", p


def ensure_first_run() -> Tuple[bool, Path]:
    """First-run flow: create the global settings dir + starter file when
    missing. Returns (created, path). Never raises (best-effort — a
    read-only home must not crash the interactive session)."""
    gp = global_settings_path()
    if gp.is_file():
        return False, gp
    try:
        gp.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(gp, _STARTER_GLOBAL)
        return True, gp
    except OSError as exc:
        _warn(f"cannot create {gp}: {exc}")
        return False, gp


def ensure_project(root: Path) -> Tuple[bool, Path]:
    """`vex config init-project`: create <root>/.vex/settings.toml when
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


def ensure_project_layout(root: Path) -> Dict[str, Any]:
    """Create the full <root>/.vex/ layout, never overwriting anything.

    Creates when missing: settings.toml (committable starter, no
    secrets), settings.local.toml (comment-only starter — zero keys,
    so the effective settings are unchanged), commands/<example>.md
    and skills/<example>/SKILL.md (only when their parent dir itself
    is new — a deliberately deleted example stays deleted).

    Returns {"path": settings.toml path, "settings_created": bool,
    "created": [relative names created under .vex/]}. Raises OSError
    on I/O failure (callers decide: `init-project` reports it, the
    session auto-scaffold swallows it — a scaffold must never crash
    the session).
    """
    d = Path(root) / ".vex"
    created: List[str] = []
    p = d / "settings.toml"
    settings_created = False
    if not p.is_file():
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(p, _STARTER_PROJECT)
        settings_created = True
        created.append("settings.toml")
    lp = d / "settings.local.toml"
    if not lp.is_file():
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(lp, _STARTER_LOCAL)
        created.append("settings.local.toml")
    cmds = d / "commands"
    if not cmds.is_dir():
        cmds.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(cmds / f"{_EXAMPLE_COMMAND_NAME}.md", _EXAMPLE_COMMAND_MD)
        created.append(f"commands/{_EXAMPLE_COMMAND_NAME}.md")
    skills = d / "skills"
    if not skills.is_dir():
        sdir = skills / _EXAMPLE_SKILL_DIR
        sdir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(sdir / "SKILL.md", _EXAMPLE_SKILL_MD)
        created.append(f"skills/{_EXAMPLE_SKILL_DIR}/SKILL.md")
    return {"path": p, "settings_created": settings_created, "created": created}


def maybe_scaffold_repo(start: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """First-`vex`-in-a-repo auto-scaffold: when `start`/CWD is inside
    a git repo, ensure its .vex/ layout exists (settings.toml +
    settings.local.toml + commands/ + skills/ examples) and its
    .gitignore covers the local file.

    Never overwrites, never scaffolds outside a git repo, never
    raises (best-effort — a read-only checkout must not crash the
    session). Returns the ensure_project_layout info dict (+
    "gitignore" outcome, "root") when inside a repo, None otherwise.

    $VEX_PROJECT_DIR (which points AT the .vex dir) overrides repo
    detection — its parent is scaffolded even without a .git entry
    (test isolation / portable layouts).
    """
    try:
        env = os.environ.get("VEX_PROJECT_DIR")
        if env:
            root = Path(env).expanduser().parent
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
        _warn_once(f"scaffold|{start or Path.cwd()}", f"cannot scaffold .vex/: {exc}")
        return None


# ---------------------------------------------------------------------------
# .gitignore handling for .vex/settings.local.toml
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
    """Make sure .vex/settings.local.toml is git-ignored in the repo at
    project_root — the same automatic handling Claude Code applies to its
    settings.local.json.

    Returns "present" (already covered), "appended" (line added to an
    existing .gitignore), "created" (.gitignore created — only inside a
    git repo), or None (not a git repo: nothing to ignore from)."""
    root = Path(project_root)
    gi = root / ".gitignore"
    if gi.is_file():
        try:
            text = gi.read_text(encoding="utf-8")
        except OSError:
            return None
        if _gitignore_covers(text, _LOCAL_GITIGNORE_ENTRY):
            return "present"
        if text and not text.endswith("\n"):
            text += "\n"
        text += "# Vex local settings (personal overrides — never commit)\n"
        text += _LOCAL_GITIGNORE_ENTRY + "\n"
        try:
            _atomic_write_text(gi, text)
        except OSError:
            return None
        return "appended"
    if (root / ".git").exists():  # .git is a dir (or a file, for worktrees)
        try:
            _atomic_write_text(
                gi,
                "# Vex local settings (personal overrides — never commit)\n"
                + _LOCAL_GITIGNORE_ENTRY
                + "\n",
            )
        except OSError:
            return None
        return "created"
    return None


def local_is_ignored(project_root: Path) -> bool:
    """True when the local-override file is covered by the repo's
    .gitignore (used by `vex config list` to warn when it isn't)."""
    gi = Path(project_root) / ".gitignore"
    if not gi.is_file():
        return False
    try:
        return _gitignore_covers(gi.read_text(encoding="utf-8"), _LOCAL_GITIGNORE_ENTRY)
    except OSError:
        return False
