"""MCP connectors — the unified `vex mcp` registry (plugins round follow-up).

Three configuration layers name the EXTERNAL MCP servers (stdio launch
commands) Vex can consume, in ascending precedence:

    plugin manifests  ~/.config/vex/plugins/<name>/  (source "plugin:<name>")
    global settings   [mcp_servers] table in the global settings.toml
    project           <repo>/.vex/connectors.toml     (committable, no secrets)
    local             <repo>/.vex/connectors.local.toml (personal overrides)

Later layers win on label collisions (global < project < local; a
configured label always beats the same label from a plugin manifest).
`discover_mcp_servers` is the single discovery entry every consumer
uses (`vex mcp list/health`, the agent loop's label resolution) so the
precedence can never drift between surfaces.

File formats: every layer is a TOML `[mcp_servers]` table mapping
labels to launch commands:

    [mcp_servers]
    linter = "python -m mcp_server"
    docs = "npx -y docs-mcp-server --root ."

The project file is committable BY DESIGN (no secrets — same rule as
`.vex/settings.toml`); personal overrides and secrets belong in the
global file or `connectors.local.toml` (git-ignored like
settings.local.toml — never committed).

Global writes go through the EXISTING tier machinery in
cli.vexconfig (imported, not reimplemented): the global file is read
with `_read_settings`, serialized with `_dump_toml`, written with
`_atomic_write_text` (tmp + replace, chmod 600 best-effort). A broken
global file is never overwritten (clean ConnectorError, exit 2).

Health (`check_health`) spawns each discovered server, lists its
tools, and reports ok/fail per server — never a traceback (spawn and
tool errors come back as ok=False data, the same contract as
memory.mcp_client). Secrets are masked for display (`mask_command`).

CLI surface (wired in cli/main.py):
    vex mcp add <label> -- <cmd...>   (persisted in global settings)
    vex mcp remove <label>            (from global settings)
    vex mcp list                      (merged view with source + masked cmd)
    vex mcp health                    (spawn + list-tools per server)

Everything here is best-effort at the edges: missing/unreadable/
malformed connector files degrade to "no servers from that layer",
never a raise into the caller (the CLI renders honest lines).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "ConnectorError",
    "add_server",
    "check_health",
    "discover_mcp_servers",
    "global_servers",
    "list_servers",
    "local_servers",
    "mask_command",
    "project_servers",
    "remove_server",
    "resolve_server",
    "server_commands",
    "validate_label",
]

CONNECTORS_FILE = "connectors.toml"
LOCAL_FILE = "connectors.local.toml"

_LABEL_PAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConnectorError(Exception):
    """A clean, user-facing connectors error (CLI renders it + exit 2).

    Raised for: bad labels, empty commands, unknown-server removals,
    a broken global file we refuse to overwrite. Plain language —
    the CLI prints it verbatim.
    """


def validate_label(label: str) -> str:
    """Check an MCP server label; return it or raise ConnectorError.

    Assumes label is user input (CLI arg or TOML key). Same charset
    discipline as plugin names (letters/digits/dots/dashes/
    underscores, starting alphanumeric) so labels stay TOML-safe,
    log-safe, and slash-surface-safe.
    """
    if not _LABEL_PAT.match(label or ""):
        raise ConnectorError(
            f"invalid server label {label!r} (expected letters/digits/dots/"
            "dashes/underscores, starting alphanumeric)"
        )
    return label


def _parse_toml_text(text: str) -> Optional[Dict[str, Any]]:
    """Parse TOML via tomllib (3.11+) or tomli; None on failure/no parser."""
    parsers = []
    try:
        import tomllib  # Python 3.11+

        parsers.append(tomllib)
    except ModuleNotFoundError:
        try:
            import tomli  # type: ignore

            parsers.append(tomli)
        except ImportError:
            return None
    for parser in parsers:
        try:
            return parser.loads(text)
        except ValueError:
            return None
    return None


def _read_connectors_file(p: Path) -> Dict[str, str]:
    """Read one connectors.toml file's [mcp_servers] table.

    Returns {} when the file is missing/unreadable/broken or holds no
    string-valued table — discovery must never die over a hand-edited
    file. Never raises.
    """
    try:
        if not p.is_file():
            return {}
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    data = _parse_toml_text(text)
    if not isinstance(data, dict):
        return {}
    table = data.get("mcp_servers")
    if not isinstance(table, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in table.items():
        if isinstance(k, str) and isinstance(v, str) and v.strip():
            try:
                validate_label(k)
            except ConnectorError:
                continue
            out[k] = v
    return out


def _project_dir(repo_path: Optional[str] = None) -> Optional[Path]:
    """The project's .vex/ directory for connector discovery.

    Assumes repo_path is the session's repo (its .vex/ subdir is the
    project layer); None = walk up from the CWD like the settings
    chain. Never raises.
    """
    try:
        from cli import vexconfig

        if repo_path:
            return Path(repo_path) / ".vex"
        return vexconfig.project_settings_dir()
    except Exception:
        return None


def global_servers() -> Dict[str, str]:
    """The global tier's [mcp_servers] table ({} when unset/unreadable)."""
    try:
        from cli.vexconfig import global_settings_path, load_vex_config

        data = load_vex_config(global_settings_path())
        table = data.get("mcp_servers")
        if not isinstance(table, dict):
            return {}
        out: Dict[str, str] = {}
        for k, v in table.items():
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                try:
                    validate_label(k)
                except ConnectorError:
                    continue
                out[k] = v
        return out
    except Exception:
        return {}


def project_servers(repo_path: Optional[str] = None) -> Dict[str, str]:
    """The committable <repo>/.vex/connectors.toml table ({} when absent)."""
    d = _project_dir(repo_path)
    if d is None:
        return {}
    return _read_connectors_file(d / CONNECTORS_FILE)


def local_servers(repo_path: Optional[str] = None) -> Dict[str, str]:
    """The personal <repo>/.vex/connectors.local.toml table ({} absent)."""
    d = _project_dir(repo_path)
    if d is None:
        return {}
    return _read_connectors_file(d / LOCAL_FILE)


def _plugin_servers() -> Dict[str, str]:
    """Installed ENABLED plugins' mcp_servers maps ({} when none)."""
    out: Dict[str, str] = {}
    try:
        from cli import plugins as plugins_mod

        for entry in plugins_mod.list_plugins():
            if entry.get("error") or entry.get("enabled") is False:
                continue
            servers = entry.get("mcp_servers") or {}
            if isinstance(servers, dict):
                for k, v in servers.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        out.setdefault(k, v)
    except Exception:
        pass
    return out


def discover_mcp_servers(
    repo_path: Optional[str] = None,
) -> Dict[str, Dict[str, str]]:
    """Every known MCP server as {label: {"command", "source"}}.

    Assumes repo_path is the session's repo (None = CWD walk-up).
    Precedence on collision (later wins): plugin:<name> < global <
    project < local. A disabled plugin's servers never appear. Never
    raises — a broken layer degrades to {} for that layer.
    """
    merged: Dict[str, Dict[str, str]] = {}
    try:
        for label, cmd in _plugin_servers().items():
            merged[label] = {"command": cmd, "source": "plugin"}
    except Exception:
        pass
    # Recover per-plugin source names (best-effort; "plugin" fallback).
    try:
        from cli import plugins as plugins_mod

        for entry in plugins_mod.list_plugins():
            if entry.get("error") or entry.get("enabled") is False:
                continue
            servers = entry.get("mcp_servers") or {}
            if isinstance(servers, dict):
                for k in servers:
                    if k in merged and merged[k]["source"] == "plugin":
                        merged[k]["source"] = f"plugin:{entry.get('name')}"
    except Exception:
        pass
    for layer, source in (
        (global_servers(), "global"),
        (project_servers(repo_path), "project"),
        (local_servers(repo_path), "local"),
    ):
        try:
            for label, cmd in layer.items():
                merged[label] = {"command": cmd, "source": source}
        except Exception:
            continue
    return merged


def server_commands(repo_path: Optional[str] = None) -> Dict[str, str]:
    """Label -> launch command for every discovered server (never raises)."""
    try:
        return {
            label: info["command"]
            for label, info in discover_mcp_servers(repo_path).items()
        }
    except Exception:
        return {}


def resolve_server(ref: str, repo_path: Optional[str] = None) -> Optional[str]:
    """Resolve a server reference to a launch command.

    Assumes ref is either a known label (returns its command) or a raw
    launch command (returned unchanged — `vex mcp call "python -m
    mcp_server" ...` keeps working). None/empty returns None. Never
    raises.
    """
    if not ref:
        return None
    try:
        servers = discover_mcp_servers(repo_path)
        if ref in servers:
            return servers[ref]["command"]
    except Exception:
        pass
    return ref


def list_servers(
    repo_path: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Discovered servers as [{label, command (masked), source}] sorted
    by label — what `vex mcp list` renders. Exposed for NIGHT-B's /mcp
    surface; never raises."""
    try:
        disc = discover_mcp_servers(repo_path)
    except Exception:
        return []
    out: List[Dict[str, str]] = []
    for label in sorted(disc):
        try:
            out.append(
                {
                    "label": label,
                    "command": mask_command(disc[label]["command"]),
                    "source": disc[label]["source"],
                }
            )
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Secrets masking
# ---------------------------------------------------------------------------

_SECRET_FLAG = re.compile(
    r"(?i)(api[_-]?key|api[_-]?token|secret|passwd|password|bearer|token|key|"
    r"authorization|auth[_-]?token|access[_-]?token)\s*([=:])?\s*(\S+)"
)
_SK_KEY = re.compile(r"sk-[A-Za-z0-9\-_]{8,}")


def mask_command(command: str) -> str:
    """Mask secret-looking values in a server launch command for display.

    Assumes command is a launch-command string. Replaces the VALUES of
    key/token/secret/password-style flags with *** and redacts
    sk-...-shaped tokens; everything else passes through unchanged so
    the command stays recognizable. Never raises.
    """
    try:

        def _hide(m: Any) -> str:
            sep = m.group(2) or " "
            return f"{m.group(1)}{sep}***"

        masked = _SECRET_FLAG.sub(_hide, command or "")
        masked = _SK_KEY.sub("sk-***", masked)
        return masked
    except Exception:
        return "***"


# ---------------------------------------------------------------------------
# Global-tier writes (through cli.vexconfig's existing machinery)
# ---------------------------------------------------------------------------


def add_server(label: str, command: str) -> str:
    """Persist a server label in the GLOBAL settings' [mcp_servers] table.

    Assumes label is user input (validated) and command is the full
    launch command string (non-empty). Returns the label. Raises
    ConnectorError on bad input or a broken global file (never
    overwrite a file we can't parse — the set_tier_key discipline).
    A hand-written [mcp_servers] section is preserved: when the file
    has no such section the entry is APPENDED (comments survive); only
    an update inside an existing table rewrites the file.
    """
    from cli import vexconfig

    validate_label(label)
    if not isinstance(command, str) or not command.strip():
        raise ConnectorError("server command must be a non-empty string")
    command = command.strip()
    p = vexconfig.tier_path("global")
    if p.is_file():
        data = vexconfig._read_settings(p)
        if data is None:
            raise ConnectorError(
                f"{p} is not valid TOML — fix it by hand before using "
                "`vex mcp add` (refusing to overwrite)"
            )
        table = data.get("mcp_servers")
        if table is not None and not isinstance(table, dict):
            raise ConnectorError(
                f"{p}: the 'mcp_servers' table must map label = command"
            )
        if not table:
            # No table yet: append a new section, preserving the user's
            # hand-written comments (the set_tier_key discipline).
            try:
                text = p.read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise ConnectorError(f"cannot read {p}: {exc}") from exc
            if not re.search(r"(?m)^\s*\[mcp_servers\]", text):
                if text and not text.endswith("\n"):
                    text += "\n"
                entry = (
                    f"\n[mcp_servers]\n{json.dumps(label)} = {json.dumps(command)}\n"
                )
                try:
                    p.write_text(text + entry, encoding="utf-8")
                    vexconfig._secure_settings_file(p)
                except OSError as exc:
                    raise ConnectorError(f"cannot write {p}: {exc}") from exc
                return label
        servers = dict(table or {})
        servers[label] = command
        data["mcp_servers"] = servers
        try:
            vexconfig._atomic_write_text(p, vexconfig._dump_toml(data) + "\n")
        except (ValueError, OSError) as exc:
            raise ConnectorError(f"cannot write {p}: {exc}") from exc
        return label
    # No global file yet: create it with the starter header + the table.
    data = {"mcp_servers": {label: command}}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        vexconfig._atomic_write_text(
            p, vexconfig._STARTER_GLOBAL + vexconfig._dump_toml(data) + "\n"
        )
    except (ValueError, OSError) as exc:
        raise ConnectorError(f"cannot write {p}: {exc}") from exc
    return label


def remove_server(label: str) -> str:
    """Remove a server label from the GLOBAL settings; returns the label.

    Raises ConnectorError when the label isn't configured globally (or
    the label is invalid, or the global file is broken — never rewrite
    what we can't parse). Project/local layers are NOT touched (they
    are hand-edited files; `vex mcp list` shows which layer owns a
    label so the operator knows where to look).
    """
    from cli import vexconfig

    validate_label(label)
    p = vexconfig.tier_path("global")
    if not p.is_file():
        raise ConnectorError(f"no server named {label!r} is configured")
    data = vexconfig._read_settings(p)
    if data is None:
        raise ConnectorError(f"{p} is not valid TOML — refusing to rewrite it")
    table = data.get("mcp_servers")
    if not isinstance(table, dict) or label not in table:
        raise ConnectorError(f"no server named {label!r} is configured")
    servers = dict(table)
    del servers[label]
    if servers:
        data["mcp_servers"] = servers
    else:
        del data["mcp_servers"]
    try:
        vexconfig._atomic_write_text(p, vexconfig._dump_toml(data) + "\n")
    except (ValueError, OSError) as exc:
        raise ConnectorError(f"cannot write {p}: {exc}") from exc
    return label


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def check_health(
    repo_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Spawn every discovered server, list its tools, report per server.

    Returns [{label, source, command (masked), ok, tools, error}] —
    ok=True carries the tool list, ok=False carries the error string.
    Never raises and never prints a traceback: a dead server is data,
    not an exception (the CLI renders one line per server + exit 1
    when any fail).
    """
    out: List[Dict[str, Any]] = []
    try:
        disc = discover_mcp_servers(repo_path)
    except Exception as exc:  # never raise into the CLI
        return [
            {
                "label": "?",
                "source": "?",
                "command": "?",
                "ok": False,
                "tools": [],
                "error": str(exc),
            }
        ]
    if not disc:
        return out
    try:
        from memory.mcp_client import list_mcp_tools
    except Exception as exc:
        return [
            {
                "label": label,
                "source": info.get("source", "?"),
                "command": mask_command(info.get("command", "")),
                "ok": False,
                "tools": [],
                "error": f"mcp client unavailable: {exc}",
            }
            for label, info in sorted(disc.items())
        ]
    for label in sorted(disc):
        info = disc[label]
        cmd = info.get("command", "")
        source = info.get("source", "?")
        try:
            res = list_mcp_tools(cmd)
        except Exception as exc:  # belt-and-braces: never traceback
            res = {"ok": False, "tools": [], "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(res, dict):
            res = {"ok": False, "tools": [], "error": "bad client response"}
        out.append(
            {
                "label": label,
                "source": source,
                "command": mask_command(cmd),
                "ok": bool(res.get("ok")),
                "tools": list(res.get("tools") or []),
                "error": (
                    None if res.get("ok") else str(res.get("error") or "unknown error")
                ),
            }
        )
    return out
