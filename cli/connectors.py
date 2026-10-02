"""MCP connectors — the unified `neo mcp` registry (plugins round follow-up).

Three configuration layers name the EXTERNAL MCP servers (stdio launch
commands) Neo can consume, in ascending precedence:

    plugin manifests  ~/.config/neo/plugins/<name>/  (source "plugin:<name>")
    global settings   [mcp_servers] table in the global settings.toml
    project           <repo>/.neo/connectors.toml     (committable, no secrets)
    local             <repo>/.neo/connectors.local.toml (personal overrides)

Later layers win on label collisions (global < project < local; a
configured label always beats the same label from a plugin manifest).
`discover_mcp_servers` is the single discovery entry every consumer
uses (`neo mcp list/health`, the agent loop's label resolution) so the
precedence can never drift between surfaces.

File formats: every layer is a TOML `[mcp_servers]` table mapping
labels to launch commands:

    [mcp_servers]
    linter = "python -m mcp_server"
    docs = "npx -y docs-mcp-server --root ."

The project file is committable BY DESIGN (no secrets — same rule as
`.neo/settings.toml`); personal overrides and secrets belong in the
global file or `connectors.local.toml` (git-ignored like
settings.local.toml — never committed).

Global writes go through the EXISTING tier machinery in
cli.neoconfig (imported, not reimplemented): the global file is read
with `_read_settings`, serialized with `_dump_toml`, written with
`_atomic_write_text` (tmp + replace, chmod 600 best-effort). A broken
global file is never overwritten (clean ConnectorError, exit 2).

Health (`check_health`) spawns each discovered server, lists its
tools, and reports ok/fail per server — never a traceback (spawn and
tool errors come back as ok=False data, the same contract as
memory.mcp_client). Secrets are masked for display (`mask_command`).

CLI surface (wired in cli/main.py):
    neo mcp add <label> -- <cmd...>   (persisted in global settings)
    neo mcp remove <label>            (from global settings)
    neo mcp list                      (merged view with source + masked cmd)
    neo mcp health                    (spawn + list-tools per server)

Everything here is best-effort at the edges: missing/unreadable/
malformed connector files degrade to "no servers from that layer",
never a raise into the caller (the CLI renders honest lines).
"""

from __future__ import annotations

import json
import queue
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "LOCAL_PERMISSIONS_FILE",
    "MCP_VERBS",
    "PERMISSIONS_FILE",
    "PERMISSIONS_TABLE",
    "PERMISSION_KEYS",
    "SIDE_EFFECT_CLASSES",
    "STATE_FILE",
    "ConnectorError",
    "ConnectorPermission",
    "ConnectorReceipt",
    "add_server",
    "call_tool",
    "check_health",
    "clear_permissions",
    "connector_disabled",
    "connector_permissions",
    "disable_all_connectors",
    "discover_mcp_servers",
    "enforcement_receipt",
    "global_servers",
    "list_prompts",
    "list_servers",
    "list_tools",
    "local_servers",
    "mask_command",
    "mcp_command",
    "pin_tool",
    "project_servers",
    "raw_catalog",
    "read_permissions",
    "remove_server",
    "resolve_server",
    "resolve_server_entry",
    "server_commands",
    "set_connector_enabled",
    "set_permissions",
    "validate_label",
    "write_permissions",
]

CONNECTORS_FILE = "connectors.toml"
LOCAL_FILE = "connectors.local.toml"
#: Per-connector enable/disable state and the per-run tool budget. It is a
#: SEPARATE file from ``connectors.toml`` and from the permission declarations
#: on purpose: those two are hand-written declarations a reviewer reads in a
#: diff, and this one is MUTABLE runtime state an operator flips with
#: ``/mcp disable``. Writing operational state into a committable declaration
#: file would put a transient decision in a reviewer's diff, and reading a
#: ``disabled`` marker out of ``connectors.toml`` would mean a hand-edited
#: connector could silently arrive switched off.
STATE_FILE = "connectors-state.json"
#: The closed ``/mcp`` verb vocabulary. Declared HERE, not re-derived from a
#: string comparison at each call site, so the registry in ``cli/commands.py``
#: and this dispatcher cannot disagree about which verbs exist.
MCP_VERBS: tuple[str, ...] = (
    "list",
    "add",
    "remove",
    "health",
    "call",
    "pin",
    "reconnect",
    "enable",
    "disable",
)
#: The committable, secret-free table that holds per-connector permission
#: declarations.  It lives beside ``connectors.toml`` so a reviewer sees the
#: blast radius of every connector in the same diff as the connector itself.
PERMISSIONS_TABLE = "connector_permissions"
PERMISSIONS_FILE = "connector-permissions.toml"
LOCAL_PERMISSIONS_FILE = "connector-permissions.local.toml"

#: Ordered least -> most privileged.  Mirrors ``mcp_server.namespace``; that
#: module is the enforcement implementation and this table only names the
#: vocabulary a declaration is written in.
SIDE_EFFECT_CLASSES: tuple[str, ...] = (
    "read",
    "search",
    "network",
    "mutation",
    "destructive",
)
#: A tool that declares no side-effect class lands here.  It is the most
#: conservative rung that still permits a mutating tool, and it is why a
#: connector that has to opt in to writes must say so explicitly.
DEFAULT_SIDE_EFFECT_CLASS = "mutation"
_SIDE_EFFECT_RANK = {name: index for index, name in enumerate(SIDE_EFFECT_CLASSES)}

#: The closed set of keys a permission declaration may carry.  An unknown key
#: is an error, not a silently-ignored field: a typo in a security declaration
#: must not read as "nothing was declared".
PERMISSION_KEYS: tuple[str, ...] = (
    "tools",
    "side_effect",
    "network",
    "write",
    "write_paths",
    "pins",
    "note",
)

_LABEL_PAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_LABEL_PAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConnectorError(Exception):
    """A clean, user-facing connectors error (CLI renders it + exit 2).

    Raised for: bad labels, empty commands, unknown-server removals,
    a broken global file we refuse to overwrite, and malformed permission
    declarations. Plain language — the CLI prints it verbatim.
    """


@dataclass(frozen=True)
class ConnectorPermission:
    """What ONE connector is declared to be allowed to do.

    A connector's blast radius used to be undeclared: the only statement about
    it was a launch command, and any tool that command offered was callable.
    This is the declaration, and it is deliberately explicit about the three
    axes that matter:

    ``tools``
        Which tool names the session may call. ``("*",)`` is the operator's
        explicit "whatever this server declares" decision and is reported as
        such; an empty tuple denies every tool.
    ``side_effect``
        The session ceiling applied to each tool's *declared* side-effect
        class. A tool that declares none lands on
        :data:`DEFAULT_SIDE_EFFECT_CLASS`.
    ``network``
        Host allowlist for a tool that can reach the network. Checked through
        :func:`shared.egress.egress_decision` as well as against this list, so
        an allowlist entry can never re-open a private or loopback address.
    ``write``
        Key-PRESENCE tri-state on purpose. ``None`` means the operator never
        said, and the write gate is therefore not enforced; ``False`` is the
        declaration "this connector does not write files" and a mutating tool
        is refused; ``True`` permits mutating classes and is recorded as an
        explicit capability claim. Absent is not silently treated as either
        answer, and :meth:`to_dict` reports ``write_declared: false`` so a
        reader can see the gap.
    """

    label: str
    tier: str = "project"
    tools: tuple[str, ...] = ()
    side_effect: str = DEFAULT_SIDE_EFFECT_CLASS
    network: tuple[str, ...] = ()
    write: Optional[bool] = None
    write_paths: tuple[str, ...] = ()
    pins: tuple[dict[str, Any], ...] = ()
    note: str = ""

    @property
    def declared(self) -> bool:
        """Return whether this is a real declaration (an absent one is None)."""
        return True

    @property
    def write_declared(self) -> bool:
        """Return whether the operator actually stated the write capability."""
        return self.write is not None

    @property
    def allows_any_tool(self) -> bool:
        """Return whether the declaration is the explicit ``"*"`` wildcard."""
        return "*" in self.tools

    def tool_policy(self) -> Any:
        """Return the ``mcp_server.namespace.MCPToolPolicy`` for this label.

        The policy is built from the declaration so least privilege and the
        declaration cannot drift: one object decides both which tools are
        exposed and what side-effect ceiling applies.
        """
        from mcp_server.namespace import MCPToolPolicy

        return MCPToolPolicy(
            servers={self.label: tuple(self.tools)},
            max_side_effect_class=self.side_effect,
        )

    def authorizes_network(self, host: str) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for one host this connector may reach.

        Both checks run and both must pass: the host must be in the declared
        list, and it must pass the deny-by-default egress policy. An allowlist
        entry therefore cannot re-open a private, loopback, or credentialed
        target.
        """
        from shared.egress import egress_decision

        normalized = str(host or "").strip().casefold()
        if not normalized:
            return False, "no network host was declared for this tool"
        allowed_hosts = {item.casefold() for item in self.network}
        if normalized not in allowed_hosts:
            return (
                False,
                f"host {normalized!r} is not in the declared network allowlist "
                f"{sorted(allowed_hosts)}",
            )
        decision = egress_decision(normalized, scheme="https")
        if not decision.allowed:
            return False, f"egress policy refused {normalized!r}: {decision.reason}"
        return (
            True,
            f"host {normalized!r} is declared and permitted by the egress policy",
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible, secret-free projection of the declaration."""
        return {
            "label": self.label,
            "tier": self.tier,
            "declared": True,
            "tools": ["*"] if self.allows_any_tool else list(self.tools),
            "tools_declared": bool(self.tools),
            "side_effect": self.side_effect,
            "network": list(self.network),
            "write": self.write,
            "write_declared": self.write_declared,
            "write_paths": list(self.write_paths),
            "pinned_tools": len(self.pins),
            "note": self.note,
        }


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
        if isinstance(k, str) and isinstance(v, str) and v.strip() and "\x00" not in v:
            try:
                validate_label(k)
            except ConnectorError:
                continue
            out[k] = v
    return out


def _project_dir(repo_path: Optional[str] = None) -> Optional[Path]:
    """The project's ``.neo`` directory for connector discovery."""
    try:
        from cli import neoconfig

        if repo_path:
            candidate = Path(repo_path).expanduser()
            if candidate.name == ".neo":
                return candidate
            return candidate / ".neo"
        return neoconfig.project_settings_dir()
    except Exception:
        return None


def global_servers() -> Dict[str, str]:
    """The global tier's [mcp_servers] table ({} when unset/unreadable)."""
    try:
        from cli.neoconfig import global_settings_path, load_neo_config

        data = load_neo_config(global_settings_path())
        table = data.get("mcp_servers")
        if not isinstance(table, dict):
            return {}
        out: Dict[str, str] = {}
        for k, v in table.items():
            if (
                isinstance(k, str)
                and isinstance(v, str)
                and v.strip()
                and "\x00" not in v
            ):
                try:
                    validate_label(k)
                except ConnectorError:
                    continue
                out[k] = v
        return out
    except Exception:
        return {}


def project_servers(repo_path: Optional[str] = None) -> Dict[str, str]:
    """The committable <repo>/.neo/connectors.toml table ({} when absent)."""
    d = _project_dir(repo_path)
    if d is None:
        return {}
    return _read_connectors_file(d / CONNECTORS_FILE)


def local_servers(repo_path: Optional[str] = None) -> Dict[str, str]:
    """The personal <repo>/.neo/connectors.local.toml table ({} absent)."""
    d = _project_dir(repo_path)
    if d is None:
        return {}
    return _read_connectors_file(d / LOCAL_FILE)


def _plugin_servers() -> List[Dict[str, str]]:
    """Return enabled plugin server entries with their true origin."""
    out: List[Dict[str, str]] = []
    try:
        from cli import plugins as plugins_mod

        for entry in plugins_mod.list_plugins():
            if entry.get("error") or entry.get("enabled") is False:
                continue
            servers = entry.get("mcp_servers") or {}
            if not isinstance(servers, dict):
                continue
            for label, command in servers.items():
                if not isinstance(label, str) or not isinstance(command, str):
                    continue
                try:
                    validate_label(label)
                except ConnectorError:
                    continue
                if command.strip():
                    out.append(
                        {
                            "label": label,
                            "command": command.strip(),
                            "source": f"plugin:{entry.get('name', 'unknown')}",
                        }
                    )
    except Exception:
        return []
    return out


def discover_mcp_servers(
    repo_path: Optional[str] = None,
) -> Dict[str, Dict[str, str]]:
    """Every known MCP server as ``{label: {command, source}}``.

    Precedence is plugin < global < project < local. Plugin selection
    and source attribution happen in one pass, so duplicate labels never
    display one plugin's command with another plugin's name.
    """
    merged: Dict[str, Dict[str, str]] = {}
    for item in _plugin_servers():
        merged.setdefault(
            item["label"],
            {"command": item["command"], "source": item["source"]},
        )
    for layer, source in (
        (global_servers(), "global"),
        (project_servers(repo_path), "project"),
        (local_servers(repo_path), "local"),
    ):
        for label, command in layer.items():
            try:
                validate_label(label)
            except ConnectorError:
                continue
            if isinstance(command, str) and command.strip():
                merged[label] = {"command": command.strip(), "source": source}
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
    launch command (returned unchanged — `neo mcp call "python -m
    mcp_server" ...` keeps working). None/empty returns None. Never
    raises.
    """
    entry = resolve_server_entry(ref, repo_path=repo_path)
    return entry["command"] if entry else None


def resolve_server_entry(
    ref: str, repo_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Resolve a server reference to ``{label, command, source, permission}``.

    This is the one resolution every enforcement surface uses, so the label a
    policy is looked up under can never differ between ``list_tools``,
    ``call_tool`` and ``check_health``. A raw launch command (no configured
    label) resolves with ``label=""`` and ``permission=None``, which is what
    makes an unlabelled invocation honest about being undeclared rather than
    silently inheriting a labelled connector's policy.

    Never raises: discovery is best-effort by contract.
    """
    if not ref:
        return None
    try:
        servers = discover_mcp_servers(repo_path)
    except Exception:
        servers = {}
    if ref in servers:
        info = servers[ref]
        return {
            "label": ref,
            "command": info.get("command", ""),
            "source": info.get("source", "?"),
            "permission": connector_permissions(ref, repo_path=repo_path),
        }
    return {
        "label": "",
        "command": str(ref),
        "source": "inline",
        "permission": None,
    }


def list_servers(
    repo_path: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Discovered servers as [{label, command (masked), source}] sorted
    by label — what `neo mcp list` renders. Exposed for NIGHT-B's /mcp
    surface; never raises.

    Each row also carries the ADDITIVE ``permissions`` / ``permissions_declared``
    fields so the listing says which connectors have a declared blast radius
    and which are running undeclared. ``permissions_declared: false`` must not
    read as "checked and clean"."""
    try:
        disc = discover_mcp_servers(repo_path)
    except Exception:
        return []
    try:
        declared = read_permissions(repo_path)
    except Exception:
        declared = {}
    out: List[Dict[str, str]] = []
    for label in sorted(disc):
        try:
            permission = declared.get(label)
            out.append(
                {
                    "label": label,
                    "command": mask_command(disc[label]["command"]),
                    "source": disc[label]["source"],
                    "permissions_declared": "true" if permission else "false",
                    "permissions": json.dumps(
                        permission.to_dict() if permission else {},
                        sort_keys=True,
                    ),
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
    """Mask secret-looking values in a launch command for display."""
    try:
        from cli.neoconfig import redact_text

        text = str(command or "")
        masked = _SECRET_FLAG.sub(
            lambda match: f"{match.group(1)}{match.group(2) or ' '}***",
            text,
        )
        masked = _SK_KEY.sub("sk-***", masked)
        return redact_text(masked)
    except Exception:
        return "***"


# ---------------------------------------------------------------------------
# Global-tier writes (through cli.neoconfig's existing machinery)
# ---------------------------------------------------------------------------


def _connector_file_for_tier(tier: str, repo_path: Optional[str] = None) -> Path:
    if tier not in ("project", "local"):
        raise ConnectorError(f"unknown connector tier: {tier!r}")
    directory = _project_dir(repo_path)
    if directory is None:
        raise ConnectorError(
            f"no .neo project directory found for connector tier {tier!r}"
        )
    return directory / (LOCAL_FILE if tier == "local" else CONNECTORS_FILE)


def _connector_table_from_file(p: Path) -> Optional[Dict[str, str]]:
    if not p.is_file():
        return {}
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConnectorError(f"cannot read {p}: {exc}") from exc
    data = _parse_toml_text(text)
    if not isinstance(data, dict):
        raise ConnectorError(f"{p} is not valid TOML — refusing to overwrite")
    table = data.get("mcp_servers", {})
    if not isinstance(table, dict):
        raise ConnectorError(f"{p}: mcp_servers must map labels to commands")
    clean: Dict[str, str] = {}
    for label, command in table.items():
        if not isinstance(label, str) or not isinstance(command, str):
            raise ConnectorError(f"{p}: connector labels and commands must be strings")
        validate_label(label)
        clean[label] = command
    return clean


def _write_connector_file(p: Path, servers: Dict[str, str], tier: str) -> None:
    from cli import neoconfig

    if p.is_symlink():
        raise ConnectorError(
            f"refusing to write connector settings through symlink: {p}"
        )
    header = (
        "# Neo MCP connector examples.\n# Add servers under [mcp_servers].\n"
        if tier == "project"
        else "# Neo local MCP connector overrides. This file is git-ignored.\n"
    )
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ConnectorError(f"cannot read {p}: {exc}") from exc
        data = _parse_toml_text(text)
        if not isinstance(data, dict):
            raise ConnectorError(f"{p} is not valid TOML — refusing to overwrite")
        if "mcp_servers" not in data:
            if text and not text.endswith("\n"):
                text += "\n"
            block = _dump_connector_table(servers)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text + "\n" + block, encoding="utf-8")
            neoconfig._secure_settings_file(p)
            return
        data["mcp_servers"] = servers
        neoconfig._atomic_write_text(p, neoconfig._dump_toml(data) + "\n")
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    neoconfig._atomic_write_text(p, header + _dump_connector_table(servers))


def _dump_connector_table(servers: Dict[str, str]) -> str:
    lines = ["[mcp_servers]"]
    lines.extend(
        f"{json.dumps(label)} = {json.dumps(command)}"
        for label, command in sorted(servers.items())
    )
    return "\n".join(lines) + "\n"


def add_server(
    label: str,
    command: str,
    tier: str = "global",
    repo_path: Optional[str] = None,
    *,
    tools: Optional[Tuple[str, ...]] = None,
    side_effect: Optional[str] = None,
    network: Optional[Tuple[str, ...]] = None,
    write: Optional[bool] = None,
) -> str:
    """Persist a server label in the selected connector tier AND declare it.

    ``global`` stores the table in settings.toml. ``project`` and ``local``
    store it in the selected repository's connector TOML file. Existing files
    are parsed before rewrite and symlinks are refused.

    A connector is **DECLARED BY DEFAULT**: unless the caller supplies an
    explicit declaration, this writes ``tools = ["*"]``,
    ``side_effect = "mutation"``, ``network = []`` and an UNDECLARED ``write``
    (the key is simply absent, so the write gate is not enforced and
    ``write_declared: false`` is what a reviewer sees). Those values reproduce
    the pre-declaration behaviour exactly, so adding a connector can never
    silently break an existing script — but the connector's blast radius is
    now a *statement in a file* that ``neo mcp list``, ``neo mcp health``,
    ``neo mcp permissions`` and ``doctor`` all show, and
    ``neo mcp permissions`` can narrow it. A connector that reaches the
    network must be given its hosts; one that writes files must be given
    ``write = true``; neither is assumed.
    """
    _add_server_command(label, command, tier=tier, repo_path=repo_path)
    set_permissions(
        label,
        tier=tier,
        repo_path=repo_path,
        tools=tools if tools is not None else ("*",),
        side_effect=side_effect,
        network=network,
        write=write,
    )
    return label


def _add_server_command(
    label: str,
    command: str,
    tier: str = "global",
    repo_path: Optional[str] = None,
) -> str:
    """Write ONLY the launch command for a connector label (no declaration)."""
    from cli import neoconfig

    validate_label(label)
    if not isinstance(command, str) or not command.strip():
        raise ConnectorError("server command must be a non-empty string")
    if "\x00" in command or any(ord(char) < 32 for char in command):
        raise ConnectorError("server command contains control characters")
    if len(command) > 16_000:
        raise ConnectorError("server command is too long")
    command = command.strip()
    if tier != "global":
        path = _connector_file_for_tier(tier, repo_path)
        servers = _connector_table_from_file(path) or {}
        servers[label] = command
        _write_connector_file(path, servers, tier)
        if tier == "local":
            neoconfig.ensure_gitignore(path.parent.parent)
        return label
    p = neoconfig.tier_path("global")
    if p.is_symlink():
        raise ConnectorError(
            f"refusing to write connector settings through symlink: {p}"
        )
    if p.is_file():
        data = neoconfig._read_settings(p)
        if data is None:
            raise ConnectorError(
                f"{p} is not valid TOML — fix it by hand before using "
                "`neo mcp add` (refusing to overwrite)"
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
                    neoconfig._secure_settings_file(p)
                except OSError as exc:
                    raise ConnectorError(f"cannot write {p}: {exc}") from exc
                return label
        servers = dict(table or {})
        servers[label] = command
        data["mcp_servers"] = servers
        try:
            neoconfig._atomic_write_text(p, neoconfig._dump_toml(data) + "\n")
        except (ValueError, OSError) as exc:
            raise ConnectorError(f"cannot write {p}: {exc}") from exc
        return label
    # No global file yet: create it with the starter header + the table.
    data = {"mcp_servers": {label: command}}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        neoconfig._atomic_write_text(
            p, neoconfig._STARTER_GLOBAL + neoconfig._dump_toml(data) + "\n"
        )
    except (ValueError, OSError) as exc:
        raise ConnectorError(f"cannot write {p}: {exc}") from exc
    return label


def remove_server(
    label: str,
    tier: str = "global",
    repo_path: Optional[str] = None,
) -> str:
    """Remove a server label from one explicit connector tier."""
    from cli import neoconfig

    validate_label(label)
    if tier != "global":
        path = _connector_file_for_tier(tier, repo_path)
        servers = _connector_table_from_file(path) or {}
        if label not in servers:
            raise ConnectorError(
                f"no server named {label!r} is configured in the {tier} tier"
            )
        del servers[label]
        _write_connector_file(path, servers, tier)
        return label
    p = neoconfig.tier_path("global")
    if not p.is_file():
        raise ConnectorError(f"no server named {label!r} is configured")
    data = neoconfig._read_settings(p)
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
        neoconfig._atomic_write_text(p, neoconfig._dump_toml(data) + "\n")
    except (ValueError, OSError) as exc:
        raise ConnectorError(f"cannot write {p}: {exc}") from exc
    return label


# ---------------------------------------------------------------------------
# Permission declarations — the connector's declared blast radius
# ---------------------------------------------------------------------------


def _permission_file_for_tier(
    tier: str, repo_path: Optional[str] = None
) -> Optional[Path]:
    """Return the permissions file for a tier, or None when global."""
    if tier not in ("project", "local"):
        raise ConnectorError(f"unknown connector tier: {tier!r}")
    directory = _project_dir(repo_path)
    if directory is None:
        raise ConnectorError(
            f"no .neo project directory found for connector tier {tier!r}"
        )
    return directory / (LOCAL_PERMISSIONS_FILE if tier == "local" else PERMISSIONS_FILE)


def _permission_table_from_file(p: Path) -> Dict[str, Any]:
    """Read one permissions file's ``[connector_permissions]`` table.

    Returns ``{}`` for a missing file. Raises :class:`ConnectorError` for a
    file that exists and cannot be parsed: silently treating an unparseable
    security declaration as "no declarations" is exactly the silent failure
    this layer exists to prevent.
    """
    if not p.is_file():
        return {}
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConnectorError(f"cannot read {p}: {exc}") from exc
    data = _parse_toml_text(text)
    if not isinstance(data, dict):
        raise ConnectorError(f"{p} is not valid TOML — refusing to interpret it")
    table = data.get(PERMISSIONS_TABLE, {})
    if not isinstance(table, dict):
        raise ConnectorError(
            f"{p}: [{PERMISSIONS_TABLE}] must map a connector label to a table"
        )
    return {
        str(label): dict(value)
        for label, value in table.items()
        if isinstance(value, dict)
    }


def render_permission_document(table: Dict[str, Any]) -> str:
    """Render the whole ``[connector_permissions]`` document deterministically.

    This module owns the rendering rather than reusing ``cli.neoconfig``'s
    settings serializer, for one reason: a declaration carries a nested table
    and an ARRAY OF TABLES (``pins``), and the settings serializer is a flat
    scalar serializer that raises on both. A security declaration whose own
    writer cannot write it is a declaration nobody can pin a tool with.
    """
    lines = [
        "# Neo connector permission declarations.",
        "# Each label states what that MCP server may be asked to do.",
        "# Hand edits are READ back (keys the reader knows are honoured); the next",
        "# `neo mcp permissions` write re-renders this document, dropping comments.",
    ]
    if not table:
        return "\n".join(lines) + "\n"
    lines.append(f"[{PERMISSIONS_TABLE}]")
    for label in sorted(table):
        lines.append(f"[{PERMISSIONS_TABLE}.{label}]")
        entry = table[label] or {}
        lines.append(f"tools = {json.dumps(list(entry.get('tools') or ['*']))}")
        lines.append(
            f"side_effect = {json.dumps(str(entry.get('side_effect') or DEFAULT_SIDE_EFFECT_CLASS))}"
        )
        if entry.get("network"):
            lines.append(f"network = {json.dumps(list(entry['network']))}")
        if entry.get("write") is not None:
            lines.append(f"write = {'true' if entry['write'] else 'false'}")
        if entry.get("write_paths"):
            lines.append(f"write_paths = {json.dumps(list(entry['write_paths']))}")
        pins = entry.get("pins") or []
        for pin in pins:
            lines.append(
                f"[[{PERMISSIONS_TABLE}.{label}.pins]]\n"
                f"tool = {json.dumps(str(pin.get('tool') or ''))}\n"
                f"definition_digest = {json.dumps(str(pin.get('definition_digest') or ''))}"
            )
        if entry.get("note"):
            lines.append(f"note = {json.dumps(str(entry['note']))}")
    return "\n".join(lines) + "\n"


def _write_permission_table(p: Path, table: Dict[str, Any]) -> None:
    """Write the ``[connector_permissions]`` document atomically."""
    from cli import neoconfig

    if p.is_symlink():
        raise ConnectorError(f"refusing to write permissions through symlink: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    neoconfig._atomic_write_text(p, render_permission_document(table))


def _global_permission_file() -> Path:
    """Return the global tier's permission file.

    The global tier gets its own FILE rather than a table inside
    ``settings.toml`` for one concrete reason: a declaration carries nested
    tables and an array of tables, and the settings serializer is a flat
    scalar writer that raises on both. A declaration whose own writer cannot
    write it is a declaration nobody can pin a tool with.
    """
    from cli.neoconfig import global_settings_path

    return global_settings_path().parent / PERMISSIONS_FILE


def _global_permission_table() -> Dict[str, Any]:
    """The global tier's permission table ({} when unset/unreadable)."""
    path = _global_permission_file()
    if not path.is_file():
        return {}
    return _permission_table_from_file(path)


def _coerce_tools(value: Any) -> Tuple[str, ...]:
    """Coerce the declared tool list; accepts a list or a comma/pipe string."""
    if value is None:
        return ()
    if isinstance(value, str):
        items = [item.strip() for item in value.replace("|", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        raise ConnectorError("permission 'tools' must be a list or a string")
    return tuple(item for item in items if item)


def _coerce_hosts(value: Any, *, key: str) -> Tuple[str, ...]:
    """Coerce the declared host list; a host must be a bare hostname."""
    items = _coerce_tools(value)
    out: List[str] = []
    for item in items:
        host = item.casefold()
        if "/" in host or "://" in host:
            raise ConnectorError(
                f"permission {key!r} entries must be bare hostnames, got {item!r}"
            )
        if host not in out:
            out.append(host)
    return tuple(out)


def _coerce_side_effect(value: Any) -> str:
    """Coerce the declared side-effect ceiling; unknown values fail closed."""
    text = str(value or "").strip().casefold()
    if not text:
        return DEFAULT_SIDE_EFFECT_CLASS
    if text not in _SIDE_EFFECT_RANK:
        raise ConnectorError(
            f"permission 'side_effect' must be one of {list(SIDE_EFFECT_CLASSES)}, "
            f"got {value!r}"
        )
    return text


def _coerce_write(value: Any) -> Optional[bool]:
    """Coerce the write tri-state. Absent stays None (key-presence)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in ("true", "yes", "allow", "1"):
        return True
    if text in ("false", "no", "deny", "0"):
        return False
    raise ConnectorError(f"permission 'write' must be true or false, got {value!r}")


def permission_from_mapping(
    label: str, value: Dict[str, Any], *, tier: str
) -> ConnectorPermission:
    """Validate and build one declaration. Raises on any malformed field."""
    unknown = sorted(str(key) for key in value if str(key) not in PERMISSION_KEYS)
    if unknown:
        raise ConnectorError(
            f"connector {label!r}: unknown permission key(s) {unknown}; "
            f"expected any of {list(PERMISSION_KEYS)}"
        )
    pins: List[Dict[str, Any]] = []
    raw_pins = value.get("pins")
    if raw_pins is not None:
        if not isinstance(raw_pins, (list, tuple)):
            raise ConnectorError(
                f"connector {label!r}: permission 'pins' must be a list of tables"
            )
        for item in raw_pins:
            if not isinstance(item, dict):
                raise ConnectorError(
                    f"connector {label!r}: each permission pin must be a table"
                )
            pins.append(
                {
                    "tool": str(item.get("tool") or ""),
                    "definition_digest": str(item.get("definition_digest") or ""),
                }
            )
    return ConnectorPermission(
        label=label,
        tier=tier,
        tools=_coerce_tools(value.get("tools")),
        side_effect=_coerce_side_effect(value.get("side_effect")),
        network=_coerce_hosts(value.get("network"), key="network"),
        write=_coerce_write(value.get("write")),
        write_paths=_coerce_hosts(value.get("write_paths"), key="write_paths"),
        pins=tuple(pins),
        note=str(value.get("note") or ""),
    )


def read_permissions(repo_path: Optional[str] = None) -> Dict[str, ConnectorPermission]:
    """Every declared connector permission, merged global < project < local.

    Returns ``{}`` when nothing is declared. A malformed file raises
    :class:`ConnectorError` — never a partial map, because a reader that sees
    "connector X has no declared permissions" when the file is broken would
    conclude the connector is unrestricted.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    project_path = _permission_file_for_tier("project", repo_path)
    local_path = _permission_file_for_tier("local", repo_path)
    project_table = (
        _permission_table_from_file(project_path) if project_path is not None else {}
    )
    local_table = (
        _permission_table_from_file(local_path) if local_path is not None else {}
    )
    for label, value in _global_permission_table().items():
        if not _LABEL_PAT.match(label or ""):
            continue
        order.append(label)
        merged[label] = dict(value)
    tiers: Dict[str, str] = {label: "global" for label in merged}
    for label, value in project_table.items():
        if not _LABEL_PAT.match(label or ""):
            continue
        if label not in merged:
            order.append(label)
        merged[label] = dict(value)
        tiers[label] = "project"
    for label, value in local_table.items():
        if not _LABEL_PAT.match(label or ""):
            continue
        if label not in merged:
            order.append(label)
        merged[label] = dict(value)
        tiers[label] = "local"
    out: Dict[str, ConnectorPermission] = {}
    for label in order:
        out[label] = permission_from_mapping(
            label, merged[label], tier=tiers.get(label, "project")
        )
    return out


def connector_permissions(
    label: str, repo_path: Optional[str] = None
) -> Optional[ConnectorPermission]:
    """Return the declaration for one label, or ``None`` when undeclared.

    ``None`` is the load-bearing answer: an undeclared connector is NOT
    enforced, and every surface that reports enforcement says so explicitly
    rather than implying protection that is not there.
    """
    if not label:
        return None
    try:
        return read_permissions(repo_path).get(label)
    except ConnectorError:
        raise
    except Exception:
        return None


def write_permissions(
    permissions: Dict[str, ConnectorPermission],
    tier: str = "project",
    repo_path: Optional[str] = None,
) -> str:
    """Persist declarations into one tier's file. Returns the file path."""
    if tier == "global":
        path = _global_permission_file()
    else:
        path = _permission_file_for_tier(tier, repo_path)
        assert path is not None  # _permission_file_for_tier raises otherwise
    _write_permission_table(
        path,
        {
            label: _permission_to_mapping(permission)
            for label, permission in sorted(permissions.items())
        },
    )
    if tier == "local":
        from cli import neoconfig

        neoconfig.ensure_gitignore(path.parent.parent)
    return str(path)


def _permission_to_mapping(permission: ConnectorPermission) -> Dict[str, Any]:
    """Render one declaration as a TOML-safe mapping."""
    out: Dict[str, Any] = {"tools": list(permission.tools)}
    out["side_effect"] = permission.side_effect
    if permission.network:
        out["network"] = list(permission.network)
    if permission.write is not None:
        out["write"] = bool(permission.write)
    if permission.write_paths:
        out["write_paths"] = list(permission.write_paths)
    if permission.pins:
        out["pins"] = [dict(pin) for pin in permission.pins]
    if permission.note:
        out["note"] = permission.note
    return out


def set_permissions(
    label: str,
    *,
    tier: str = "project",
    repo_path: Optional[str] = None,
    tools: Optional[Tuple[str, ...]] = None,
    side_effect: Optional[str] = None,
    network: Optional[Tuple[str, ...]] = None,
    write: Optional[bool] = None,
    write_paths: Optional[Tuple[str, ...]] = None,
    note: str = "",
) -> ConnectorPermission:
    """Merge one declaration into a tier and return the stored result.

    ``tools``/``network``/``write_paths`` REPLACE the stored list when
    supplied; ``side_effect``/``write``/``note`` replace when supplied. A
    field the caller does not name is left exactly as it was, so a caller can
    narrow one axis without restating the rest.
    """
    validate_label(label)
    if tier == "global":
        stored = dict(_global_permission_table())
    else:
        path = _permission_file_for_tier(tier, repo_path)
        stored = dict(_permission_table_from_file(path) if path is not None else {})
    current = stored.get(label, {})
    merged = dict(current)
    if tools is not None:
        merged["tools"] = list(tools)
    if side_effect is not None:
        merged["side_effect"] = _coerce_side_effect(side_effect)
    if network is not None:
        merged["network"] = list(network)
    if write is not None:
        merged["write"] = bool(write)
    if write_paths is not None:
        merged["write_paths"] = list(write_paths)
    if note:
        merged["note"] = note
    stored[label] = merged
    permission = permission_from_mapping(label, merged, tier=tier)
    existing = {
        name: permission_from_mapping(name, value, tier=tier)
        for name, value in stored.items()
        if name != label and _LABEL_PAT.match(name or "")
    }
    existing[label] = permission
    write_permissions(existing, tier=tier, repo_path=repo_path)
    return permission


def clear_permissions(
    label: str, *, tier: str = "project", repo_path: Optional[str] = None
) -> str:
    """Remove one label's declaration. Returns the label."""
    validate_label(label)
    if tier == "global":
        path = _global_permission_file()
    else:
        path = _permission_file_for_tier(tier, repo_path)
    stored = dict(_permission_table_from_file(path) if path is not None else {})
    if label not in stored:
        raise ConnectorError(f"connector {label!r} has no {tier}-tier permission entry")
    del stored[label]
    _write_permission_table(path, stored)
    return label


@dataclass(frozen=True)
class ConnectorReceipt:
    """The recorded outcome of one connector enforcement decision.

    Every ``list_tools`` / ``call_tool`` return carries one of these under
    ``receipt`` so a reviewer can answer "was this connector declared, what
    does it say it may do, and what actually happened" without reading the
    source. ``enforced`` is the field that matters: ``False`` means the call
    happened with NO declaration behind it, which must never be mistaken for
    "checked and allowed".
    """

    label: str = ""
    declared: bool = False
    enforced: bool = False
    allowed: bool = True
    reason: str = ""
    side_effect: str = ""
    declared_tools: Tuple[str, ...] = ()
    network: Tuple[str, ...] = ()
    write_declared: bool = False
    write: Optional[bool] = None
    pinned: bool = False
    pin_status: str = "not_pinned"
    tool: str = ""
    namespaced_name: str = ""
    definition_digest: str = ""
    reviews: Tuple[Dict[str, Any], ...] = ()
    hooks: Tuple[Dict[str, Any], ...] = ()
    #: The per-run tool budget AFTER this decision. Present on ``call_tool`` so
    #: the receipt answers "what did this run spend on MCP" without a second
    #: read, and present as an empty dict on the read-only surfaces where no
    #: call happened - a key that is present-and-empty rather than absent,
    #: because a missing key reads as "nobody measured it".
    budget: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible, secret-free receipt."""
        return {
            "label": self.label,
            "declared": bool(self.declared),
            "enforced": bool(self.enforced),
            "allowed": bool(self.allowed),
            "reason": self.reason,
            "side_effect": self.side_effect,
            "declared_tools": list(self.declared_tools),
            "network": list(self.network),
            "write_declared": bool(self.write_declared),
            "write": self.write,
            "pinned": bool(self.pinned),
            "pin_status": self.pin_status,
            "tool": self.tool,
            "namespaced_name": self.namespaced_name,
            "definition_digest": self.definition_digest,
            "untrusted_reviews": [dict(item) for item in self.reviews],
            "hooks": [dict(item) for item in self.hooks],
            "budget": dict(self.budget),
        }


def enforcement_receipt(
    permission: Optional[ConnectorPermission],
    *,
    allowed: bool = True,
    reason: str = "",
    tool: str = "",
    namespaced_name: str = "",
    definition_digest: str = "",
    pin_status: str = "not_pinned",
    reviews: Tuple[Dict[str, Any], ...] = (),
    hooks: Tuple[Dict[str, Any], ...] = (),
    budget: Optional[Dict[str, Any]] = None,
) -> ConnectorReceipt:
    """Build a :class:`ConnectorReceipt` from a declaration and a decision."""
    return ConnectorReceipt(
        label=permission.label if permission else "",
        declared=permission is not None,
        enforced=permission is not None,
        allowed=allowed,
        reason=reason
        or (
            "no permission declaration for this connector; the call was NOT gated"
            if permission is None
            else "permitted by the connector's declared permissions"
        ),
        side_effect=permission.side_effect if permission else "",
        declared_tools=permission.tools if permission else (),
        network=permission.network if permission else (),
        write_declared=bool(permission.write_declared) if permission else False,
        write=permission.write if permission else None,
        pinned=bool(permission.pins) if permission else False,
        pin_status=pin_status,
        tool=tool,
        namespaced_name=namespaced_name,
        definition_digest=definition_digest,
        reviews=reviews,
        hooks=hooks,
        budget=dict(budget or {}),
    )


def _tool_definitions(label: str, tools: List[Any]) -> Tuple[Any, ...]:
    """Build ``ToolDefinition`` rows for one server's raw tool descriptors."""
    from mcp_server.namespace import tools_for_server

    return tools_for_server(label, tools)


def _run_bounded(
    fn,
    timeout_s: float,
    timeout_message: str = "health check timed out",
) -> Dict[str, Any]:
    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put({"value": fn()})
        except Exception as exc:  # pragma: no cover - defensive boundary
            result_queue.put({"error": exc})

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    worker.join(max(0.01, float(timeout_s)))
    if worker.is_alive():
        return {"ok": False, "tools": [], "error": timeout_message}
    try:
        result = result_queue.get_nowait()
    except queue.Empty:
        return {"ok": False, "tools": [], "error": "health check returned no result"}
    if "error" in result:
        return {
            "ok": False,
            "tools": [],
            "error": f"{type(result['error']).__name__}: {result['error']}",
        }
    value = result.get("value")
    return (
        value
        if isinstance(value, dict)
        else {"ok": False, "tools": [], "error": "bad client response"}
    )


def _declared_network_hosts(definition: Any) -> List[str]:
    """Read the hosts a catalogued tool says it can reach.

    MCP servers state this in their input schema, so both shapes are read: a
    bare ``networkDomains`` key at the schema root (what a flat descriptor
    carries) and the JSON-Schema ``properties.networkDomains`` form with its
    ``const``/``default``/``enum`` value. A tool that declares none yields
    ``[]``, and the caller treats that as a refusal rather than a guess.
    """
    schema = getattr(definition, "input_schema", None)
    if not isinstance(schema, Mapping):
        return []
    raw = schema.get("networkDomains") or schema.get("network_domains")
    if raw is None:
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            node = properties.get("networkDomains") or properties.get("network_domains")
            if isinstance(node, Mapping):
                raw = node.get("const")
                if raw is None:
                    raw = node.get("default")
                if raw is None:
                    options = node.get("enum")
                    raw = (
                        options[0]
                        if isinstance(options, (list, tuple)) and options
                        else None
                    )
            else:
                raw = node
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if str(item).strip()]
    return [str(raw)]


def _gate_tool(
    permission: Optional[ConnectorPermission],
    definition: Any,
) -> Tuple[bool, str]:
    """Return ``(allowed, reason)`` for one catalogued tool.

    The order is deliberate and matches ``mcp_server.namespace``: least
    privilege FIRST (server configured, tool exposed, side-effect ceiling),
    then the connector's own write and network declarations, then nothing
    else. A caller must never learn "your tool was refused because its digest
    moved" when the real reason is that the tool was never permitted.
    """
    if permission is None:
        return (
            True,
            "no permission declaration for this connector; the call was NOT gated",
        )
    try:
        definition = permission.tool_policy().authorize(definition)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    effect = definition.side_effect_class
    if (
        permission.write is False
        and _SIDE_EFFECT_RANK.get(effect, _SIDE_EFFECT_RANK[DEFAULT_SIDE_EFFECT_CLASS])
        >= _SIDE_EFFECT_RANK["mutation"]
    ):
        return (
            False,
            f"connector {permission.label!r} declares write=false, so tool "
            f"{definition.namespaced} (side effect {effect!r}) may not be called",
        )
    if _SIDE_EFFECT_RANK.get(effect, 0) == _SIDE_EFFECT_RANK["network"]:
        # Only a tool that declares the `network` class is gated on the host
        # allowlist. A `mutation` tool MAY reach the network, but the operator
        # already stated the ceiling it is allowed to operate under via
        # ``side_effect``; requiring every mutating tool to also name a host
        # would make a legitimate write-only connector unusable and would train
        # operators to declare hosts they do not have.
        hosts = _declared_network_hosts(definition)
        if not hosts:
            return (
                False,
                f"tool {definition.namespaced} can reach the network but declares no "
                f"host, and connector {permission.label!r} declares network="
                f"{list(permission.network)}",
            )
        for host in hosts[:16]:
            allowed, reason = permission.authorizes_network(str(host))
            if not allowed:
                return False, f"tool {definition.namespaced}: {reason}"
    return (
        True,
        f"permitted by connector {permission.label!r}: tools="
        f"{list(permission.tools)} side_effect<={permission.side_effect} "
        f"write={permission.write}",
    )


def _gate_pins(
    permission: Optional[ConnectorPermission], catalog: Sequence[Any], name: str
) -> Tuple[bool, str, str]:
    """Re-hash the live catalog against the recorded pins. Returns ``(ok, reason, status)``.

    Least privilege already ran, so a tool the session may not call never
    reaches here. A pinned tool whose definition moved — or that vanished —
    is refused: both mean "the thing that was approved is not the thing that
    would run".
    """
    if permission is None or not permission.pins:
        return True, "", "not_pinned"
    from mcp_server.namespace import ToolPinSet

    pins = ToolPinSet(
        [
            {
                "server": permission.label,
                "tool": str(pin.get("tool") or ""),
                "definition_digest": str(pin.get("definition_digest") or ""),
            }
            for pin in permission.pins
        ]
    )
    try:
        pins.verify(catalog)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", "pin_mismatch"
    return True, f"{len(pins)} tool definition(s) unchanged since approval", "pinned"


def _hook_engine(repo_path: Optional[str], config: Any = None) -> Any:
    """Build (or reuse) the user-hook engine for one repository.

    Returns ``None`` when the hook layer cannot be loaded — not importable, or
    a hook configuration that refuses to parse. Both are reported by
    ``neo hooks list``, ``doctor`` and the connector receipt, and neither may
    break the connector surface: an unusable hook LAYER is a missing
    declaration, and a missing declaration must not become a crash.

    The engine's own per-event fail policy governs an individual hook, and that
    is enforced below through :meth:`extensions.user_hooks.HookEngine.gate`.
    """
    global _HOOK_ENGINE_CACHE
    if config is not None:
        try:
            from extensions.user_hooks import HookEngine

            return HookEngine(config, repo_path=repo_path)
        except Exception:
            return None
    key = str(repo_path or "")
    cached = _HOOK_ENGINE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from extensions.user_hooks import HookEngine, load_hook_config

        engine = HookEngine(load_hook_config(repo_path=repo_path), repo_path=repo_path)
    except Exception:
        return None
    _HOOK_ENGINE_CACHE[key] = engine
    return engine


_HOOK_ENGINE_CACHE: Dict[str, Any] = {}
_SESSION_STARTED: set = set()


_HOOKS_UNAVAILABLE: Tuple[Dict[str, Any], ...] = (
    {
        "event": "-",
        "hooks_unavailable": True,
        "note": "the extensions user-hook layer is not importable; no hook gate ran",
    },
)


def _hook_subject(
    *,
    label: str,
    namespaced: str,
    side_effect: str,
    tool: str = "",
    repo_path: Optional[str] = None,
) -> Any:
    """Project a connector call into the hook subject vocabulary.

    Returns ``None`` when the hook layer is not importable (an install that
    does not ship ``extensions``). The caller treats that as "no hook layer",
    which is reported in the receipt as ``hooks_unavailable`` rather than
    crashing the connector surface.
    """
    try:
        from extensions.user_hooks import HookSubject
    except Exception:
        return None
    return HookSubject(
        tool=namespaced or tool,
        mcp_server=label,
        side_effect_class=side_effect,
        task_id="",
        session_id="",
        path=str(repo_path or ""),
    )


def _fire_hooks(
    event: str,
    subject: Any,
    *,
    repo_path: Optional[str],
    gates: bool,
) -> Tuple[Optional[Any], List[Dict[str, Any]]]:
    """Fire one user-hook event; return ``(gate_or_None, receipts)``.

    ``gates=True`` asks for the fail-policy-resolved answer (the caller must
    honour it). ``gates=False`` fires an observational event whose only
    effects are the receipt rows.

    Returns ``(None, [])`` when no engine could be built, so a missing or
    broken hook configuration is indistinguishable from "no hooks configured"
    to the connector surface — and is reported as such by ``neo hooks list``
    and the connector receipt rather than being swallowed.
    """
    if subject is None:
        return None, [dict(item) for item in _HOOKS_UNAVAILABLE]
    engine = _hook_engine(repo_path)
    if engine is None:
        return None, [dict(item) for item in _HOOKS_UNAVAILABLE]
    try:
        if gates:
            gate = engine.gate(event, subject)
            return gate, [gate.to_dict()]
        outcome = engine._dispatch(_event_enum(event), subject)
        return None, [outcome.to_dict()]
    except Exception as exc:  # a hook layer must never take the call down
        return None, [{"event": event, "error": f"{type(exc).__name__}: {exc}"}]


def _event_enum(event: str) -> Any:
    from extensions.user_hooks import HookEvent

    return HookEvent(event)


def _namespaced_rows(
    label: str, tools: List[Any], permission: Optional[ConnectorPermission]
) -> Tuple[List[Dict[str, Any]], List[str], List[Any]]:
    """Project a server's tools into namespaced catalog rows.

    Returns ``(rows, blocked_names, definitions)``. With a declaration in
    place the rendered catalog IS the callable catalog, because the blocked
    tools are reported separately and are not in ``rows``.
    """
    definitions = _tool_definitions(label, tools) if label else ()
    rows: List[Dict[str, Any]] = []
    blocked: List[str] = []
    for definition in definitions:
        allowed, _reason = _gate_tool(permission, definition)
        if not allowed:
            blocked.append(definition.namespaced)
            continue
        row = definition.as_dict()
        if not row["name"].startswith("mcp__"):
            row["name"] = definition.namespaced
        rows.append(row)
    if not definitions and tools:
        # An unlabelled invocation (a raw launch command) has no server name
        # to namespace against; report the raw names rather than pretending.
        for item in tools:
            name = item.get("name") if isinstance(item, dict) else str(item)
            rows.append(
                {
                    "name": str(name or "?"),
                    "server": "",
                    "tool": str(name or "?"),
                    "description": redact_tool_description(item),
                    "side_effect_class": DEFAULT_SIDE_EFFECT_CLASS,
                    "definition_digest": "",
                }
            )
    return rows, blocked, list(definitions)


def redact_tool_description(item: Any) -> str:
    """Return a bounded, redacted description for a raw tool descriptor."""
    try:
        from cli.neoconfig import redact_text

        text = (
            str((item or {}).get("description") or "") if isinstance(item, dict) else ""
        )
        return redact_text(text)[:_MAX_DESCRIPTION_CHARS]
    except Exception:
        return ""


_MAX_DESCRIPTION_CHARS = 2_000


def list_tools(
    ref: str,
    repo_path: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: float = 60.0,
    *,
    config: Optional[Mapping[str, Any]] = None,
    include_schemas: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Resolve and list tools with a bounded connector call.

    The returned mapping KEEPS its historical ``{"ok", "tools", "error"}``
    shape and ADDS ``namespaced``, ``blocked``, ``receipt``, and (VEX-CS-07)
    ``schemas``, ``prompts``, ``prompts_error``, ``deferral`` and ``budget``.
    Tool names are the namespaced ``mcp__<server>__<tool>`` identifiers common
    clients route on, and when the connector carries a permission declaration
    the visible list is exactly the callable list - a refused tool is reported
    under ``blocked`` with the reason and never rendered as available.

    **Deferred schemas.** Rows are the model-facing deferred form: name,
    description and side-effect class, and NO ``inputSchema``. A schema is
    loaded only for the tools named in ``include_schemas``, and ``deferral``
    carries the measured before/after token receipt. This is the reference
    client's deferral, and it is the reason a 40-tool server does not cost 40
    schemas on every turn.

    **Budget.** ``budget`` records the exposure of THIS listing against the
    per-run ceilings, so ``/mcp <label>`` can answer "how much of the window is
    this server taking" without a second registry read.
    """
    entry = resolve_server_entry(ref, repo_path=repo_path) or {
        "label": "",
        "command": str(ref or ""),
        "source": "inline",
        "permission": None,
    }
    label = entry["label"]
    permission = entry["permission"]

    # -- the operator's own switches, before anything is spawned -------------
    if connector_disabled(label, repo_path=repo_path):
        return {
            "ok": False,
            "tools": [],
            "blocked": [],
            "schemas": [],
            "prompts": [],
            "namespaced": bool(label),
            "error": (
                f"connector {label!r} is disabled; re-enable it with "
                f"`/mcp enable {label}`"
            ),
            "receipt": enforcement_receipt(
                permission,
                allowed=False,
                reason=f"connector {label!r} is switched off by the operator",
            ).to_dict(),
        }
    strict = strict_undeclared_reason(config, label=label)
    if strict is not None and permission is None:
        return {
            "ok": False,
            "tools": [],
            "blocked": [],
            "schemas": [],
            "prompts": [],
            "namespaced": bool(label),
            "error": strict,
            "receipt": enforcement_receipt(
                None,
                allowed=False,
                reason=strict,
            ).to_dict(),
        }
    try:
        raw_tools, probe = _catalog_read(
            entry, repo_path=repo_path, cwd=cwd, timeout_s=timeout_s
        )
        if not probe.get("ok"):
            return {
                "ok": False,
                "tools": [],
                "blocked": [],
                "schemas": [],
                "prompts": [],
                "namespaced": bool(label),
                "error": str(probe.get("error") or "list-tools failed"),
                "receipt": enforcement_receipt(permission).to_dict(),
            }
        rows, blocked, definitions = _namespaced_rows(label, raw_tools, permission)
        rows, blocked = _narrow_to_pins(rows, blocked, permission)
        deferred = _defer_rows(rows, blocked, definitions)
        # The schemas, the deferral receipt and the prompt commands are read
        # from the RAW catalog when the raw read answered, and from the client
        # descriptors otherwise. Both are stated in the receipt's
        # ``schema_source`` rather than being assumed.
        schema_descriptors = list(probe.get("raw_tools") or []) or list(raw_tools)
        schema_source = str(probe.get("schema_source") or "client-normalized")
        schemas: List[Dict[str, Any]] = []
        if include_schemas:
            schemas = _load_requested_schemas(
                label, schema_descriptors, include_schemas
            )
        prompt_rows: List[Dict[str, Any]] = []
        prompts_error = probe.get("prompts_error")
        if label and probe.get("prompts"):
            try:
                from mcp_server.tool_pinning import prompt_commands

                prompt_rows = [
                    dict(row)
                    for row in prompt_commands(label, probe.get("prompts") or [])
                ]
            except Exception:
                prompt_rows = []
        budget = _budget_for_listing(label, deferred)
        deferral = _deferral_receipt(label, schema_descriptors, include_schemas)
        deferral["schema_source"] = schema_source
        deferral["digest_source"] = "client-normalized"
        deferral["digest_source_note"] = (
            "definition digests are computed from the client descriptors in BOTH "
            "arms, because call_tool re-hashes the same source; only the schema "
            "BYTES come from the raw read"
        )
        return {
            "ok": True,
            "tools": deferred,
            "blocked": blocked,
            "schemas": schemas,
            "prompts": prompt_rows,
            "prompts_error": prompts_error,
            "schema_source": schema_source,
            "namespaced": bool(label),
            "error": None,
            "deferral": deferral,
            "budget": budget,
            "receipt": enforcement_receipt(
                permission,
                reason=(
                    f"{len(deferred)} of {len(deferred) + len(blocked)} declared tool(s) are "
                    "callable under this connector's permissions"
                    if permission is not None
                    else "no permission declaration for this connector; the tool list "
                    "was NOT filtered"
                ),
                budget=budget,
            ).to_dict(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "tools": [],
            "blocked": [],
            "schemas": [],
            "prompts": [],
            "namespaced": False,
            "error": f"{type(exc).__name__}: {exc}",
            "receipt": enforcement_receipt(None).to_dict(),
        }


def _narrow_to_pins(
    rows: Sequence[Dict[str, Any]],
    blocked: Sequence[str],
    permission: Optional[ConnectorPermission],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Drop every tool the connector has NOT pinned out of the EXPOSED catalog.

    Two different declarations narrow two different things, and conflating them
    is how a catalog ends up showing something the session may not call:

    ``tools``
        the connector's blast radius - what the session may CALL. Enforced by
        ``MCPToolPolicy.authorize`` on the call path and reported under
        ``blocked`` by :func:`_namespaced_rows`.
    ``pins``
        the definitions an operator actually APPROVED. A connector that pinned
        one tool has not approved the other thirty-nine, so the others are not
        EXPOSED - they move from the rendered catalog to ``blocked``.

    This narrows the SURFACE only. The call path is unchanged, because the pin
    gate there (``_gate_pins``) re-hashes the pinned digests and must see the
    WHOLE live catalog: narrowing the catalog it verifies would let a server
    make a pinned tool vanish by being narrower.

    A connector with NO pins is untouched, so the shipped default is unchanged.
    """
    if permission is None or not permission.pins:
        return [dict(row) for row in rows], [str(name) for name in blocked]
    pinned = {str(pin.get("tool") or "") for pin in permission.pins}
    kept: List[Dict[str, Any]] = []
    dropped: List[str] = [str(name) for name in blocked]
    for row in rows:
        if str(row.get("tool") or "") in pinned:
            kept.append(dict(row))
        else:
            dropped.append(str(row.get("name") or "?"))
    return kept, dropped


def _defer_rows(
    rows: Sequence[Any], blocked: Sequence[str], definitions: Sequence[Any]
) -> List[Dict[str, Any]]:
    """Return the model-facing deferred rows for one server's VISIBLE catalog.

    ``definitions`` arrives UNFILTERED from :func:`_namespaced_rows` - it is the
    whole catalog, because the pin re-hash needs every tool, not only the
    callable ones. Re-projecting it without dropping the blocked names rendered
    a refused tool in the catalog AND listed it as blocked, which is precisely
    the drift ``tests/test_r2_16_extension_ops.py::test_the_namespace_layer_
    refuses_an_undeclared_tool_on_a_real_server`` exists to catch. Filtering
    here is what keeps "the catalog a client renders is the catalog it may call"
    true of the DEFERRED rows too.

    The rows are projected from the ORIGINAL definitions rather than re-derived
    from ``as_dict()``, because ``as_dict()`` omits ``inputSchema`` and a digest
    recomputed without the schema would be a DIFFERENT digest beside the one
    :meth:`ToolPinSet.verify` re-checks - two answers to one question, on the
    axis that must never have two.

    Falls back to the plain rows when the server could not be namespaced at all
    (an inline launch command has no server to namespace against, and inventing
    one would fabricate an identifier the client never published).
    """
    blocked_names = set(blocked or ())
    if definitions:
        try:
            from mcp_server.tool_pinning import summary_for

            return [
                summary_for(definition).as_dict()
                for definition in definitions
                if definition.namespaced not in blocked_names
            ]
        except Exception:
            return [dict(row) for row in rows]
    return [dict(row) for row in rows]


def _load_requested_schemas(
    label: str, tools: Sequence[Any], requested: Sequence[str]
) -> List[Dict[str, Any]]:
    """Load the ``inputSchema`` for the named tools and nothing else.

    Reads the schema off the :class:`mcp_server.namespace.ToolDefinition`
    directly rather than through its ``as_dict()``, because ``as_dict()`` does
    not carry the schema - that omission IS the deferral - so going through it
    would load the requested schema and then throw it away and report an empty
    one. A receipt that claims a schema was loaded while returning ``{}`` is
    worse than not loading it.
    """
    try:
        from mcp_server.namespace import parse_namespaced_tool_name, tools_for_server
        from mcp_server.tool_pinning import estimate_tokens

        definitions = {
            definition.tool: definition for definition in tools_for_server(label, tools)
        }
        out: List[Dict[str, Any]] = []
        for name in requested:
            tool = str(name)
            if tool.startswith("mcp__"):
                _, tool = parse_namespaced_tool_name(tool)
            definition = definitions.get(tool)
            if definition is None:
                out.append({"error": f"{tool!r} is not offered by server {label!r}"})
                continue
            schema = dict(definition.input_schema or {})
            out.append(
                {
                    "name": definition.namespaced,
                    "inputSchema": schema,
                    "tokens": estimate_tokens(schema),
                }
            )
        return out
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]


def _deferral_receipt(
    label: str, tools: Sequence[Any], requested: Optional[Sequence[str]]
) -> Dict[str, Any]:
    """Return the before/after token receipt for this catalog's deferral."""
    try:
        from mcp_server.tool_pinning import measure_schema_deferral

        return measure_schema_deferral(label, tools, requested=list(requested or []))
    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "tool_count": len(list(tools or [])),
            "estimator": "unavailable",
        }


def _budget_for_listing(label: str, rows: Sequence[Any]) -> Dict[str, Any]:
    """Return the per-run budget receipt for one catalog exposure.

    The budget counts the MODEL-facing form (name, description, side-effect
    class) and the tool count, and surfaces NO digest: a digest recomputed from
    a schema-free row would be a second, different answer to "what is this
    tool's definition digest" beside the one :func:`list_tools` publishes. Two
    digests for one tool is exactly the drift the pin gate cannot tolerate, so
    the budget stays out of that axis entirely.
    """
    try:
        from mcp_server.tool_pinning import ToolBudget

        budget = ToolBudget()
        budget.record_exposure(label, [dict(row) for row in rows])
        return budget.as_dict()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "within_budget": False}


#: Per-process run budgets, keyed by nothing: a CLI process serves one session,
#: and a REPL session is one run. Exposed as a module global so
#: ``/mcp <label>`` and ``/mcp call`` report the SAME accumulated numbers rather
#: than two fresh zeroed budgets - two budgets would each report zero calls and
#: a reader would be told the run made none.
_RUN_BUDGETS: Dict[str, Any] = {}


def run_budget(
    config: Optional[Mapping[str, Any]] = None, *, reset: bool = False
) -> Any:
    """Return this process's :class:`mcp_server.tool_pinning.ToolBudget`.

    Counters accumulate across the calls of one session; the CEILINGS are
    re-read from ``config`` on every call. Both halves matter and they fail in
    opposite directions if you get them wrong: caching the ceilings means a
    config that tightened the budget mid-session is ignored (a gate that cannot
    close), while resetting the counters per call means a budget that reports
    zero calls after two of them (a receipt that cannot be believed).

    The ceilings come from :func:`budget_from_config`, which reads them by key
    presence - see its docstring for why none of these keys is in
    ``harness.config.DEFAULTS``. ``reset=True`` clears the counters, which is
    what a new session or a test wants.
    """
    from mcp_server.tool_pinning import budget_from_config

    if reset:
        _RUN_BUDGETS.clear()
    budget = _RUN_BUDGETS.get("budget")
    if budget is None:
        budget = budget_from_config(config)
        _RUN_BUDGETS["budget"] = budget
        return budget
    fresh = budget_from_config(config)
    budget.max_tools_exposed = fresh.max_tools_exposed
    budget.max_calls = fresh.max_calls
    budget.chars_per_token = fresh.chars_per_token
    budget.notes = fresh.notes
    return budget


def strict_undeclared_reason(
    config: Optional[Mapping[str, Any]] = None,
    *,
    strict: Optional[bool] = None,
    label: str = "",
) -> Optional[str]:
    """Return a refusal reason when strict mode refuses, else ``None``.

    Delegates to :func:`mcp_server.tool_pinning.strict_undeclared_reason`, which
    owns the rule. The DEFAULT is ``None``: an undeclared connector runs with
    ``enforced: false`` and a reason saying the call was NOT gated, exactly as
    R2-16 shipped it. Strict mode is opt-in through the config key
    ``mcp_require_declared_connector`` and is read by KEY PRESENCE, so an
    absent key is "the operator said nothing" and never a refusal.
    """
    try:
        from mcp_server.tool_pinning import strict_undeclared_reason as _reason

        return _reason(config, strict=strict, label=label)
    except Exception:
        if strict:
            return (
                f"connector {label or '(inline command)'!r} has no permission "
                "declaration and strict mode is on"
            )
        return None


def call_tool(
    ref: str,
    tool: str,
    args: Optional[Dict[str, Any]] = None,
    repo_path: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: float = 60.0,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Call one connector tool behind the declared-permission gate.

    The pre-call order is: resolve the server, refuse a disabled connector,
    evaluate the ``PreToolUse`` hook gate (fail-closed for that event), fetch the
    live catalog, authorize least privilege against the declaration, re-hash the
    live catalog against any recorded approval pins, dispatch the call, and
    review the RESULT as untrusted content. The returned mapping keeps
    ``{"ok", "text", "error"}`` and ADDS ``receipt``, ``tool``, and
    ``namespaced``.

    Two VEX-CS-07 checks sit OUTSIDE that order on purpose, and neither weakens
    it:

    * **strict mode** (opt-in, default off) refuses an UNDECLARED connector
      before a server is spawned. It is a refusal that the default never makes,
      so it cannot have relaxed anything.
    * **the per-run budget** is consulted after the gate and refuses the call
      that would cross a declared ceiling, then records the call it allowed. The
      budget rides on ``receipt["budget"]`` so the run's MCP spend is readable
      from the receipt that already exists.
    """
    entry = resolve_server_entry(ref, repo_path=repo_path) or {
        "label": "",
        "command": str(ref or ""),
        "source": "inline",
        "permission": None,
    }
    label = entry["label"]
    permission = entry["permission"]

    # -- 0. the operator's own switches, before anything is spawned -----------
    if connector_disabled(label, repo_path=repo_path):
        return {
            "ok": False,
            "text": "",
            "error": (
                f"connector {label!r} is disabled; re-enable it with "
                f"`/mcp enable {label}`"
            ),
            "tool": str(tool or ""),
            "namespaced": str(tool or ""),
            "receipt": enforcement_receipt(
                permission,
                allowed=False,
                reason=f"connector {label!r} is switched off by the operator",
            ).to_dict(),
        }
    strict = strict_undeclared_reason(config, label=label)
    if strict is not None and permission is None:
        return {
            "ok": False,
            "text": "",
            "error": strict,
            "tool": str(tool or ""),
            "namespaced": str(tool or ""),
            "receipt": enforcement_receipt(
                None,
                allowed=False,
                reason=strict,
            ).to_dict(),
        }
    try:
        from mcp_server.namespace import namespaced_tool_name, review_tool_result

        namespaced = namespaced_tool_name(label or "inline", tool) if tool else ""
    except Exception:
        namespaced = str(tool or "")
        namespaced_tool_name = None  # type: ignore[assignment]
        review_tool_result = None  # type: ignore[assignment]
    hook_receipts: List[Dict[str, Any]] = []

    # -- 1. the user-hook gate, before anything is spawned -------------------
    # The subject's side_effect_class is the connector's DECLARED ceiling, not
    # the live tool's: reading the live class would require spawning the server
    # before the gate has decided, which is exactly what a PreToolUse gate
    # exists to prevent.
    declared_side_effect = (
        permission.side_effect if permission is not None else DEFAULT_SIDE_EFFECT_CLASS
    )
    subject = _hook_subject(
        label=label,
        namespaced=namespaced,
        side_effect=declared_side_effect,
        tool=str(tool or ""),
        repo_path=repo_path,
    )
    if label:
        if label not in _SESSION_STARTED:
            _SESSION_STARTED.add(label)
            _, rows = _fire_hooks(
                "SessionStart", subject, repo_path=repo_path, gates=False
            )
            hook_receipts.extend(rows)
        gate, rows = _fire_hooks("PreToolUse", subject, repo_path=repo_path, gates=True)
        hook_receipts.extend(rows)
        if gate is not None and not gate.allowed:
            _, failure_rows = _fire_hooks(
                "PostToolUseFailure", subject, repo_path=repo_path, gates=False
            )
            hook_receipts.extend(failure_rows)
            return {
                "ok": False,
                "text": "",
                "error": f"refused by a PreToolUse user hook: {gate.reason}",
                "tool": str(tool or ""),
                "namespaced": namespaced,
                "blocked_by": gate.block_by,
                "receipt": enforcement_receipt(
                    permission,
                    allowed=False,
                    reason=f"refused by a PreToolUse user hook ({gate.block_by}): {gate.reason}",
                    tool=str(tool or ""),
                    namespaced_name=namespaced,
                    hooks=tuple(hook_receipts),
                ).to_dict(),
            }

    try:
        from memory.mcp_client import call_mcp_tool

        # -- 2. the live catalog, for least privilege and pin re-hashing ----
        # The SAME reader ``list_tools`` uses, so a digest recorded from the
        # listing and a digest re-hashed here can never be two different
        # answers to one question.
        catalog: List[Any] = []
        digest = ""
        blocked_reason = ""
        allowed = True
        pin_status = "not_pinned"
        if label:
            raw_tools, listing = _catalog_read(
                entry, repo_path=repo_path, cwd=cwd, timeout_s=timeout_s
            )
            if not listing.get("ok"):
                return {
                    "ok": False,
                    "text": "",
                    "error": (
                        "cannot read the server's tool catalog, so the call was "
                        f"refused: {listing.get('error')}"
                    ),
                    "tool": str(tool or ""),
                    "namespaced": namespaced,
                    "receipt": enforcement_receipt(
                        permission,
                        allowed=False,
                        reason="catalog unavailable; a declared connector is never called blind",
                        tool=str(tool or ""),
                        namespaced_name=namespaced,
                        hooks=tuple(hook_receipts),
                    ).to_dict(),
                }
            _, _, definitions = _namespaced_rows(label, raw_tools, None)
            catalog = definitions
            definition = next(
                (item for item in catalog if item.tool == str(tool or "")),
                None,
            )
            if definition is None:
                allowed = False
                blocked_reason = (
                    f"tool {tool!r} is not offered by server {label!r} "
                    f"(offered: {[item.tool for item in catalog]})"
                )
            else:
                allowed, blocked_reason = _gate_tool(permission, definition)
                digest = definition.digest
            pin_ok, pin_reason, pin_status = _gate_pins(permission, catalog, namespaced)
            if allowed and not pin_ok:
                allowed = False
                blocked_reason = pin_reason
        if not allowed:
            _, failure_rows = _fire_hooks(
                "PostToolUseFailure", subject, repo_path=repo_path, gates=False
            )
            hook_receipts.extend(failure_rows)
            return {
                "ok": False,
                "text": "",
                "error": blocked_reason
                or "refused by the connector's declared permissions",
                "tool": str(tool or ""),
                "namespaced": namespaced,
                "receipt": enforcement_receipt(
                    permission,
                    allowed=False,
                    reason=blocked_reason,
                    tool=str(tool or ""),
                    namespaced_name=namespaced,
                    definition_digest=digest,
                    pin_status=pin_status,
                    hooks=tuple(hook_receipts),
                ).to_dict(),
            }

        # -- 3. the per-run tool budget, then dispatch -------------------------
        # The budget is a GATE, not a report: it refuses the call that would
        # cross a declared ceiling, and it records only a call it allowed. A
        # refused call leaves the counters untouched, so the receipt can never
        # report its own refusal as consumption.
        budget = run_budget(config)
        try:
            budget.record_exposure(label or "inline", list(raw_tools) if label else [])
        except Exception:
            pass
        decision = budget.record_call(label or "inline", str(tool or ""))
        if not decision.allowed:
            _, failure_rows = _fire_hooks(
                "PostToolUseFailure", subject, repo_path=repo_path, gates=False
            )
            hook_receipts.extend(failure_rows)
            return {
                "ok": False,
                "text": "",
                "error": decision.reason,
                "tool": str(tool or ""),
                "namespaced": namespaced,
                "receipt": enforcement_receipt(
                    permission,
                    allowed=False,
                    reason=decision.reason,
                    tool=str(tool or ""),
                    namespaced_name=namespaced,
                    definition_digest=digest,
                    pin_status=pin_status,
                    hooks=tuple(hook_receipts),
                    budget=budget.as_dict(),
                ).to_dict(),
            }

        result = _run_bounded(
            lambda: call_mcp_tool(entry["command"], tool, args or {}, cwd=cwd),
            max(0.01, min(float(timeout_s), 300.0)),
            "connector call timed out",
        )
        if not result.get("ok"):
            _, failure_rows = _fire_hooks(
                "PostToolUseFailure", subject, repo_path=repo_path, gates=False
            )
            hook_receipts.extend(failure_rows)
            return {
                "ok": False,
                "text": "",
                "error": str(result.get("error") or "connector call failed"),
                "tool": str(tool or ""),
                "namespaced": namespaced,
                "receipt": enforcement_receipt(
                    permission,
                    allowed=False,
                    reason="the server returned an error for this call",
                    tool=str(tool or ""),
                    namespaced_name=namespaced,
                    definition_digest=digest,
                    pin_status=pin_status,
                    hooks=tuple(hook_receipts),
                    budget=budget.as_dict(),
                ).to_dict(),
            }

        # -- 4. the RESULT is untrusted content ------------------------------
        reviews: List[Dict[str, Any]] = []
        text = str(result.get("text") or "")
        if label and review_tool_result is not None:
            usable, review = review_tool_result(
                text, server=label, tool=str(tool or "")
            )
            text = usable
            reviews.append(dict(review.as_dict()))

        _, post_rows = _fire_hooks(
            "PostToolUse", subject, repo_path=repo_path, gates=False
        )
        hook_receipts.extend(post_rows)
        return {
            "ok": True,
            "text": text,
            "error": None,
            "tool": str(tool or ""),
            "namespaced": namespaced,
            "receipt": enforcement_receipt(
                permission,
                allowed=True,
                reason=blocked_reason
                or "permitted by the connector's declared permissions",
                tool=str(tool or ""),
                namespaced_name=namespaced,
                definition_digest=digest,
                pin_status=pin_status,
                reviews=tuple(reviews),
                hooks=tuple(hook_receipts),
                budget=budget.as_dict(),
            ).to_dict(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "text": "",
            "error": f"{type(exc).__name__}: {exc}",
            "tool": str(tool or ""),
            "namespaced": namespaced,
            "receipt": enforcement_receipt(
                permission,
                allowed=False,
                reason=f"{type(exc).__name__}: {exc}",
                tool=str(tool or ""),
                namespaced_name=namespaced,
                hooks=tuple(hook_receipts),
            ).to_dict(),
        }


def check_health(
    repo_path: Optional[str] = None,
    timeout_s: float = 60.0,
) -> List[Dict[str, Any]]:
    """Probe each server with a bounded timeout and return safe results.

    A slow, dead, or malformed server is represented as ``ok=False``;
    no traceback or raw command secret reaches the caller.
    """
    from cli.neoconfig import redact_text

    try:
        disc = discover_mcp_servers(repo_path)
    except Exception as exc:
        return [
            {
                "label": "?",
                "source": "?",
                "command": "?",
                "ok": False,
                "tools": [],
                "error": redact_text(exc),
            }
        ]
    if not disc:
        return []
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
                "error": redact_text(f"mcp client unavailable: {exc}"),
            }
            for label, info in sorted(disc.items())
        ]
    timeout = max(0.01, min(float(timeout_s), 300.0))
    out: List[Dict[str, Any]] = []
    try:
        declared = read_permissions(repo_path)
    except Exception as exc:
        declared = {}
        permission_error = f"{type(exc).__name__}: {exc}"
    else:
        permission_error = ""
    for label in sorted(disc):
        info = disc[label]
        command = info.get("command", "")
        permission = declared.get(label)
        res = _run_bounded(lambda command=command: list_mcp_tools(command), timeout)
        if not isinstance(res, dict):
            res = {"ok": False, "tools": [], "error": "bad client response"}
        blocked: List[str] = []
        visible = list(res.get("tools") or [])
        if permission is not None and res.get("ok"):
            rows, blocked, _ = _namespaced_rows(label, visible, permission)
            visible = rows
        prompt_rows: List[Dict[str, Any]] = []
        auto_disabled = _record_hang(label, res.get("error"), repo_path=repo_path)
        if res.get("ok") and not connector_disabled(label, repo_path):
            try:
                prompt_rows = list(list_prompts(label, repo_path=repo_path))["commands"]
            except Exception:
                prompt_rows = []
        out.append(
            {
                "label": label,
                "source": info.get("source", "?"),
                "command": mask_command(command),
                "ok": bool(res.get("ok")),
                "tools": visible,
                "blocked_tools": blocked,
                "prompts": prompt_rows,
                "disabled": connector_disabled(label, repo_path),
                "auto_disabled_reason": auto_disabled,
                "permissions_declared": permission is not None,
                "permissions_error": permission_error or None,
                "permissions": permission.to_dict() if permission else {},
                "error": None
                if res.get("ok")
                else redact_text(res.get("error") or "unknown error"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# VEX-CS-07 - the /mcp verb surface, deferred schemas, and the run budget
# ---------------------------------------------------------------------------
#
# `call_tool` above is the pre-call boundary and its ORDER is load-bearing; this
# section does not rebuild it. It adds the four things that boundary could not
# do on its own, and each is additive:
#
#   * a mutable enable/disable state, so a wedging server can be taken out of
#     the registry instead of timing out on every call for the rest of the
#     session;
#   * the RAW protocol reads (`tools/list`, `prompts/list`), which
#     `memory.mcp_client` cannot answer because it normalizes a listed tool to
#     `{name, description}` and therefore drops the `inputSchema`;
#   * the strict-mode refusal for an undeclared connector (opt-in; the default
#     is still `enforced: false`);
#   * the per-run tool budget, which is a GATE rather than a report.
#
# `mcp_command` is the ONE implementation of the nine `/mcp` verbs. A REPL
# `/mcp` and a `neo mcp` are two doors onto it, not two implementations.

#: Errors that mean "this server did not answer in time" as opposed to "this
#: command is wrong". Only a HANG disables a connector: a missing executable or
#: a malformed command is an operator error, and silently switching a connector
#: off because a typo'd path does not exist would hide the typo.
_HANG_MARKERS: Tuple[str, ...] = (
    "timed out",
    "timeout",
    "timeouterror",
)


def _is_hang(error: Any) -> bool:
    """Return whether ``error`` reads as a server that never answered.

    Substring matching on a LOWERCASED message is deliberate and its limit is
    stated: a server's own wording is not a vocabulary we control, so this can
    both over- and under-match. It is used only to decide whether to persist a
    disable, and the reason it persists carries the full message - so an
    under-match shows up as a connector that was NOT disabled, which is the
    harmless direction. An over-match disables a connector with a visible
    reason, and ``/mcp enable`` reverses it.
    """
    text = str(error or "").casefold()
    return any(marker in text for marker in _HANG_MARKERS)


# -- enable/disable state ----------------------------------------------------


def _state_path(repo_path: Optional[str] = None) -> Optional[Path]:
    """Return the mutable connector-state file for a repo, or None."""
    directory = _project_dir(repo_path)
    if directory is None:
        return None
    return directory / STATE_FILE


def _global_state_path() -> Path:
    """Return the global tier's connector-state file."""
    from cli.neoconfig import global_settings_path

    return global_settings_path().parent / STATE_FILE


def read_state(repo_path: Optional[str] = None) -> Dict[str, Any]:
    """Return the merged connector state: disabled labels, explicit re-enables.

    Global < project, so a project can disable a connector for its own
    repository without editing the operator's global file. NEVER RAISES: a
    corrupt or unreadable state file degrades to "nothing is disabled", which is
    the fail-OPEN direction and the one that is honest - and it is reported
    through :func:`connector_state_problem` rather than treated as a decision.
    """
    merged: Dict[str, Any] = {
        "disabled": {},
        "enabled": {},
        "all": False,
        "problems": [],
    }
    for path in (_global_state_path(), _state_path(repo_path)):
        if path is None or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except Exception as exc:
            merged["problems"].append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(data, dict):
            merged["problems"].append(f"{path}: state file is not an object")
            continue
        merged["all"] = bool(data.get("all")) or merged["all"]
        for key in ("disabled", "enabled"):
            bucket = data.get(key)
            if isinstance(bucket, dict):
                for label, reason in bucket.items():
                    merged[key][str(label)] = str(reason or key)
    return merged


def connector_state_problem(repo_path: Optional[str] = None) -> Optional[str]:
    """Return why the connector-state file could not be read, else ``None``.

    Separate from :func:`read_state` on purpose. "No connector is disabled" and
    "we could not tell whether a connector is disabled" are different facts, and
    a caller that renders only the first has silently converted an unreadable
    file into a decision.
    """
    problems = read_state(repo_path).get("problems") or []
    return str(problems[0]) if problems else None


def connector_disabled(label: str, repo_path: Optional[str] = None) -> bool:
    """Return whether one connector is currently switched off.

    ``all`` disables every connector that has no explicit re-enable. An explicit
    ``enabled`` entry wins over ``all``, so ``/mcp disable all`` followed by
    ``/mcp enable one`` leaves exactly one connector on - the shape an operator
    expects from a master switch.
    """
    if not label:
        return False
    state = read_state(repo_path)
    if label in (state.get("disabled") or {}):
        return True
    if label in (state.get("enabled") or {}):
        return False
    return bool(state.get("all"))


def _write_state(state: Dict[str, Any], repo_path: Optional[str] = None) -> str:
    """Write the project-tier state file atomically. Returns the path."""
    from cli import neoconfig

    path = _state_path(repo_path) or _global_state_path()
    if path.is_symlink():
        raise ConnectorError(
            f"refusing to write connector state through symlink: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "all": bool(state.get("all")),
        "disabled": dict(sorted((state.get("disabled") or {}).items())),
        "enabled": dict(sorted((state.get("enabled") or {}).items())),
    }
    try:
        neoconfig._atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
    except (ValueError, OSError) as exc:
        raise ConnectorError(f"cannot write {path}: {exc}") from exc
    return str(path)


def set_connector_enabled(
    label: str,
    enabled: bool,
    *,
    repo_path: Optional[str] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """Switch one connector on or off and return the recorded state.

    Disabling is NOT removal: the declaration, the permission entry and the
    launch command all stay exactly where they are, so ``/mcp enable`` restores
    the previous blast radius without the operator retyping it. That is why
    this is a state file rather than a delete.

    An unknown label raises :class:`ConnectorError` rather than writing a
    disable for a connector that does not exist - a state file that grows
    entries nobody declared is a file whose contents cannot be trusted. A label
    that is ALREADY disabled is accepted, so re-disabling is idempotent rather
    than an error.
    """
    validate_label(label)
    try:
        known = discover_mcp_servers(repo_path)
    except Exception:
        known = {}
    if label not in known and not connector_disabled(label, repo_path=repo_path):
        raise ConnectorError(f"no MCP connector named {label!r} is configured")
    state = read_state(repo_path)
    disabled = dict(state.get("disabled") or {})
    explicitly_on = dict(state.get("enabled") or {})
    note = reason or ("re-enabled by operator" if enabled else "disabled by operator")
    if enabled:
        disabled.pop(label, None)
        explicitly_on[label] = note
    else:
        explicitly_on.pop(label, None)
        disabled[label] = note
    path = _write_state(
        {"all": bool(state.get("all")), "disabled": disabled, "enabled": explicitly_on},
        repo_path=repo_path,
    )
    return {
        "ok": True,
        "label": label,
        "enabled": bool(enabled),
        "path": path,
        "reason": note,
        "disabled": connector_disabled(label, repo_path=repo_path),
    }


def disable_all_connectors(
    *, repo_path: Optional[str] = None, reason: str = ""
) -> Dict[str, Any]:
    """Flip the master switch. ``/mcp disable all`` reaches here.

    A master switch rather than a loop over labels on purpose: a loop would
    write N entries and would then be wrong the moment a connector is added,
    and ``/mcp disable all`` on an empty registry has to be a valid, honest
    no-op rather than a usage error.
    """
    state = read_state(repo_path)
    path = _write_state(
        {
            "all": True,
            "disabled": dict(state.get("disabled") or {}),
            "enabled": dict(state.get("enabled") or {}),
        },
        repo_path=repo_path,
    )
    return {
        "ok": True,
        "all": True,
        "path": path,
        "reason": reason or "every connector disabled by operator",
    }


def _record_hang(
    label: str, error: Any, repo_path: Optional[str] = None
) -> Optional[str]:
    """Disable a connector whose probe hung, and return the reason recorded.

    Returns ``None`` when nothing was recorded: the error is not a hang, the
    label is empty, or the state file refused the write. A server that hangs is
    REPORTED and DISABLED rather than left to time out on every subsequent call,
    because a wedge that repeats is a wedge that outlasts the session - and the
    refusal is still returned to the caller either way, so a failed write loses
    no information.
    """
    if not label or not _is_hang(error):
        return None
    reason = f"disabled automatically: probe did not answer ({str(error)[:200]})"
    try:
        set_connector_enabled(label, False, repo_path=repo_path, reason=reason)
    except ConnectorError:
        return None
    return reason


# -- raw protocol reads ------------------------------------------------------
#
# `memory.mcp_client` is another module's file and it answers exactly two
# questions: what tools does this server offer (as `{name, description}`) and
# call this tool. Two things this round needs are NOT among them:
#
#   1. the raw `inputSchema` each tool published - without it the side-effect
#      class, the `networkDomains` and the pin digest are all evaluated against
#      an EMPTY schema (the limitation both `mcp_server/AGENTS.md` and
#      `cli/AGENTS.md` already record);
#   2. `prompts/list`, which is what makes a server feel native rather than
#      bolted on.
#
# The upstream change is filed as a REQUEST with its exact shape rather than
# made here; see `cli/AGENTS.md`. Until it lands these two reads are the only
# place in the product that speaks the raw protocol for MCP, and they are
# deliberately thin: one bounded spawn, one read, no pooling, no reuse.


def _server_errlog() -> Any:
    """Return a stderr sink the SDK may write a spawned server's errors to.

    The SDK binds ``sys.stderr`` as a DEFAULT PARAMETER at import time, and
    pytest's capture replaces it with a fileno-less stream, which breaks every
    stdio spawn for the rest of the session. Passing an explicit sink is the
    documented workaround (`memory/AGENTS.md`, Round 7) and is why this helper
    exists instead of a bare ``stdio_client(params)`` call.
    """
    import sys

    stream = getattr(sys, "__stderr__", None)
    if stream is not None:
        try:
            stream.fileno()
            return stream
        except Exception:
            pass
    try:
        import os

        return open(os.devnull, "w", encoding="utf-8")
    except Exception:  # pragma: no cover - devnull opens in practice
        return None


def _child_env() -> Optional[Dict[str, str]]:
    """Return the safe inherited environment for a spawned MCP server."""
    try:
        from memory.mcp_client import _child_environment

        return dict(_child_environment(None))
    except Exception:
        return None


def _run_coroutine(operation: Any, timeout_s: float) -> Tuple[bool, Any]:
    """Run one coroutine on a private event loop and close it. Never raises.

    Delegates to ``memory.mcp_client._run_sync_bounded`` when it is importable so
    the bound, the cancellation behaviour and the loop teardown are the SAME
    implementation the rest of the product's MCP reads use; the fallback exists
    only so this module does not hard-fail on a client that renamed it.
    """
    import asyncio

    async def _drive() -> Any:
        return await operation

    try:
        from memory.mcp_client import _run_sync_bounded

        return _run_sync_bounded(_drive(), timeout_s)
    except Exception:
        pass
    loop = asyncio.new_event_loop()
    try:
        return True, loop.run_until_complete(
            asyncio.wait_for(_drive(), max(0.01, float(timeout_s)))
        )
    except Exception as exc:
        return False, exc
    finally:
        try:
            loop.close()
        except Exception:
            pass


def _as_mapping(item: Any) -> Dict[str, Any]:
    """Project one SDK protocol object onto a plain mapping. Never raises."""
    try:
        dump = getattr(item, "model_dump", None)
        if callable(dump):
            value = dump(by_alias=True, exclude_none=True)
            if isinstance(value, Mapping):
                return dict(value)
    except Exception:
        pass
    if isinstance(item, Mapping):
        return dict(item)
    out: Dict[str, Any] = {}
    for key in ("name", "description", "inputSchema", "annotations", "arguments"):
        value = getattr(item, key, None)
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif isinstance(value, Mapping):
            out[key] = dict(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [
                dict(entry) if isinstance(entry, Mapping) else entry for entry in value
            ]
        else:
            out[key] = str(value)
    return out


async def _read_raw_catalog(
    argv: List[str], cwd: Optional[str], timeout_s: float
) -> Dict[str, Any]:
    """Spawn one server, read ``tools/list`` and ``prompts/list``, and return both."""
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=argv[0], args=list(argv[1:]), env=_child_env(), cwd=cwd
    )
    async with stdio_client(params, errlog=_server_errlog()) as streams:
        async with ClientSession(*streams) as session:
            await asyncio.wait_for(session.initialize(), timeout_s)
            tools_result = await asyncio.wait_for(session.list_tools(), timeout_s)
            tools = [
                _as_mapping(item)
                for item in (getattr(tools_result, "tools", None) or [])
            ]
            prompts: List[Dict[str, Any]] = []
            prompts_error: Optional[str] = None
            try:
                prompts_result = await asyncio.wait_for(
                    session.list_prompts(), timeout_s
                )
                prompts = [
                    _as_mapping(item)
                    for item in (getattr(prompts_result, "prompts", None) or [])
                ]
            except Exception as exc:
                # A server with no prompt capability is NOT a failed probe: the
                # tool read already succeeded. Recording the reason and
                # returning the catalog keeps an optional capability from
                # discarding a working answer.
                prompts_error = f"{type(exc).__name__}: {exc}"
            return {
                "ok": True,
                "tools": tools,
                "prompts": prompts,
                "prompts_error": prompts_error,
                "error": None,
            }


def _raw_probe(
    entry: Mapping[str, Any],
    repo_path: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """One bounded raw ``tools/list`` + ``prompts/list`` read for a resolved entry.

    Split out of :func:`raw_catalog` so the catalog readers can share it without
    re-resolving the server: :func:`list_tools` and :func:`call_tool` both need
    the SAME descriptors, and resolving twice would be two registry reads of one
    decision.
    """
    label = str(entry.get("label") or "")
    command = str(entry.get("command") or "")
    timeout = max(0.01, min(float(timeout_s), 300.0))

    def probe() -> Dict[str, Any]:
        from memory.mcp_client import parse_server_command

        argv = parse_server_command(command)
        if not argv:
            return {"ok": False, "error": "empty server command"}
        completed, value = _run_coroutine(
            _read_raw_catalog(argv, cwd, timeout), timeout
        )
        if completed:
            if isinstance(value, dict):
                return value
            return {"ok": False, "error": "bad client response"}
        error = (
            f"{type(value).__name__}: {value}"
            if value is not None
            else "TimeoutError: operation timed out"
        )
        return {"ok": False, "error": error}

    try:
        result = dict(_run_bounded(probe, timeout, "connector probe timed out") or {})
    except Exception as exc:  # pragma: no cover - defensive boundary
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result.setdefault("ok", False)
    result.setdefault("tools", [])
    result.setdefault("prompts", [])
    result.setdefault("prompts_error", None)
    result.setdefault("error", None)
    result["label"] = label
    result["command"] = mask_command(command)
    result["timeout_s"] = timeout
    disabled_reason = _record_hang(label, result.get("error"), repo_path=repo_path)
    result["auto_disabled_reason"] = disabled_reason
    result["disabled"] = connector_disabled(label, repo_path=repo_path)
    return result


def _catalog_read(
    entry: Mapping[str, Any],
    repo_path: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: float = 60.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return ``(descriptors, receipt)`` for one connector, plus the raw catalog.

    ``memory.mcp_client.list_mcp_tools`` stays the source of RECORD for the tool
    rows and therefore for every definition digest, because it is what the
    product's own tests stub and what `call_tool` has always hashed. That is
    rule 8 doing its job: changing the catalog source would silently invalidate
    a recorded pin and quietly un-gate every test that injects a catalog.

    The RAW read is an ENRICHMENT beside it, and it is what the round's own
    features need:

    * ``include_schemas`` loads a real ``inputSchema``, which the client wrapper
      drops entirely;
    * the before/after deferral receipt is measured against real published
      schemas rather than against a schema-free normalization of them;
    * ``prompts/list`` exists only on the raw read;
    * a hang is detected here, so a wedging server is disabled by any surface
      that lists a connector, not only by ``/mcp reconnect``.

    The receipt carries BOTH catalogs under distinct names, so a caller can say
    which one answered. ``schema_source`` is ``"raw"`` when the raw read
    succeeded and ``"client-normalized"`` when it did not - and the digests in
    the rows are computed from the client descriptors in BOTH cases, because
    ``call_tool`` hashes the same source. One digest per tool, always.
    """
    label = str(entry.get("label") or "")
    command = str(entry.get("command") or "")
    try:
        from memory.mcp_client import list_mcp_tools

        listing = _run_bounded(
            lambda: list_mcp_tools(command, cwd=cwd),
            max(0.01, min(float(timeout_s), 300.0)),
            "connector list-tools timed out",
        )
    except Exception as exc:
        listing = {"ok": False, "tools": [], "error": f"{type(exc).__name__}: {exc}"}
    listing = dict(listing or {})
    tools = list(listing.get("tools") or [])

    raw: Dict[str, Any] = {
        "ok": False,
        "tools": [],
        "prompts": [],
        "prompts_error": None,
    }
    if label:
        raw = _raw_probe(entry, repo_path=repo_path, cwd=cwd, timeout_s=timeout_s)
    schema_source = (
        "raw" if (raw.get("ok") and raw.get("tools")) else "client-normalized"
    )
    receipt = {
        "ok": bool(listing.get("ok")),
        "error": listing.get("error"),
        "tools": tools,
        "raw_tools": list(raw.get("tools") or []),
        "raw_ok": bool(raw.get("ok")),
        "raw_error": raw.get("error"),
        "prompts": list(raw.get("prompts") or []),
        "prompts_error": raw.get("prompts_error"),
        "schema_source": schema_source,
        "disabled": connector_disabled(label, repo_path=repo_path),
        "auto_disabled_reason": raw.get("auto_disabled_reason"),
    }
    if not receipt["disabled"]:
        auto = _record_hang(label, listing.get("error"), repo_path=repo_path)
        receipt["auto_disabled_reason"] = auto or receipt["auto_disabled_reason"]
    return tools, receipt


def raw_catalog(
    ref: str,
    repo_path: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Return a server's RAW ``tools/list`` and ``prompts/list`` over stdio.

    This is the one place in the product that reads the raw protocol, and it
    exists because the client wrapper drops the ``inputSchema``. The return
    keeps the connector contract's shape - ``{"ok", ..., "error"}``, never a
    raise - and ADDS ``tools``, ``prompts``, ``prompts_error``, ``label``,
    ``command``, ``timeout_s``, ``disabled`` and ``auto_disabled_reason``.

    A probe that hangs is bounded by ``timeout_s`` AND disables the connector
    with the reason recorded. Every other failure - a missing executable, a
    malformed command, a server that refuses the prompt capability - comes back
    as data and leaves the connector exactly as it was.
    """
    entry = resolve_server_entry(ref, repo_path=repo_path) or {
        "label": "",
        "command": str(ref or ""),
        "source": "inline",
        "permission": None,
    }
    return _raw_probe(entry, repo_path=repo_path, cwd=cwd, timeout_s=timeout_s)


def list_prompts(
    ref: str,
    repo_path: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Return a server's prompts as ``/mcp__<server>__<prompt>`` command rows.

    Discovery is over the real protocol (``prompts/list``), not over a config
    file, and the rows are produced by
    :func:`mcp_server.tool_pinning.prompt_commands` - the same namespace
    normalization a tool name goes through, so a hostile prompt name cannot
    inject the separator.

    Keeps ``{"ok", "commands", "error"}`` and adds ``prompts``,
    ``prompts_error``, ``label``, ``disabled`` and ``auto_disabled_reason``. A
    server with no prompt capability returns ``ok=True`` with an empty list and
    the reason in ``prompts_error``: "this server offers no prompts" is a fact,
    not a failure, and rendering it as one would make a healthy server look
    broken.
    """
    result = raw_catalog(ref, repo_path=repo_path, cwd=cwd, timeout_s=timeout_s)
    if not result.get("ok"):
        return {
            "ok": False,
            "commands": [],
            "prompts": [],
            "error": str(result.get("error") or "prompt discovery failed"),
            "label": result.get("label", ""),
            "disabled": bool(result.get("disabled")),
            "auto_disabled_reason": result.get("auto_disabled_reason"),
        }
    try:
        from mcp_server.tool_pinning import prompt_commands

        rows = prompt_commands(
            result.get("label") or "inline", result.get("prompts") or []
        )
    except Exception as exc:
        return {
            "ok": False,
            "commands": [],
            "prompts": [],
            "error": f"{type(exc).__name__}: {exc}",
            "label": result.get("label", ""),
            "disabled": bool(result.get("disabled")),
            "auto_disabled_reason": result.get("auto_disabled_reason"),
        }
    return {
        "ok": True,
        "commands": [dict(row) for row in rows],
        "prompts": list(result.get("prompts") or []),
        "prompts_error": result.get("prompts_error"),
        "error": None,
        "label": result.get("label", ""),
        "disabled": bool(result.get("disabled")),
        "auto_disabled_reason": result.get("auto_disabled_reason"),
    }


# -- pinning -----------------------------------------------------------------


def pin_tool(
    label: str,
    tool: str,
    definition_digest: str,
    *,
    tier: str = "project",
    repo_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one tool's definition digest as the digest an approval is bound to.

    The pin is written into the connector's EXISTING ``[connector_permissions]``
    declaration rather than into a second store, because :func:`_gate_pins`
    already reads exactly that table: a second file would be a second answer to
    "is this tool pinned", and the two could disagree.

    Pinning is NOT authorization. It binds an approval to the definition the
    operator actually read; whether the tool may be CALLED is still the
    declaration's ``tools`` list and its side-effect ceiling, both of which
    ``MCPToolPolicy.authorize`` checks first.

    A pin needs an existing declaration: pinning a tool on an undeclared
    connector would create a pin against a blast radius nobody stated, which
    reads as protection that is not there.
    """
    from dataclasses import replace as _replace

    validate_label(label)
    name = str(tool or "").strip()
    if not name:
        raise ConnectorError("pin requires a tool name")
    digest = str(definition_digest or "").strip()
    if not digest:
        raise ConnectorError("pin requires the definition digest the approval saw")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ConnectorError(
            "a pin digest is the 64-hex tool definition digest from "
            "mcp_server.namespace.tool_definition_digest; refusing to record "
            f"{digest[:20]!r}"
        )
    current = connector_permissions(label, repo_path=repo_path)
    if current is None:
        raise ConnectorError(
            f"connector {label!r} has no permission declaration; declare it with "
            f"`neo mcp permissions {label} --tool <name> ...` before pinning"
        )
    pins = [dict(pin) for pin in current.pins]
    replaced = False
    for pin in pins:
        if str(pin.get("tool")) == name:
            pin["definition_digest"] = digest
            replaced = True
    if not replaced:
        pins.append({"tool": name, "definition_digest": digest})
    updated = _replace(current, tier=tier, pins=tuple(pins))
    others = {
        other: permission
        for other, permission in read_permissions(repo_path).items()
        if other != label
    }
    path = write_permissions({**others, label: updated}, tier=tier, repo_path=repo_path)
    return {
        "ok": True,
        "label": label,
        "tool": name,
        "namespaced_name": f"mcp__{label}__{name}",
        "definition_digest": digest,
        "pinned_tools": len(pins),
        "replaced": replaced,
        "path": path,
        "tier": tier,
    }


# -- the /mcp verb surface ---------------------------------------------------


def _render_mcp_lines(repo_path: Optional[str] = None, label: str = "") -> List[str]:
    """Return the plain lines ``/mcp`` and ``/mcp <label>`` render.

    Delegates to :func:`list_servers` and :func:`list_tools` rather than
    re-reading the registry, so the slash surface and ``neo mcp list`` are one
    implementation. Every line is PLAIN text: a connector label, a launch
    command and a tool description are all DATA that crosses into a markup
    parser, so a hostile label must be escaped by the CALLER rather than
    neutralised here by a guess about which caller it is.
    """
    if label:
        listing = list_tools(label, repo_path=repo_path)
        receipt = listing.get("receipt") or {}
        if not listing.get("ok"):
            return [
                f"{label}: {listing.get('error') or 'could not list tools'}",
                f"  declared: {bool(receipt.get('declared'))} "
                f"({receipt.get('reason', '')})",
            ]
        lines = [f"{label}: {len(listing.get('tools') or [])} callable tool(s)"]
        for row in listing.get("tools") or []:
            lines.append(
                f"  {row.get('name', '?')}  {row.get('side_effect_class', '?')}"
                f"  {row.get('description', '')}".rstrip()
            )
        for blocked in listing.get("blocked") or []:
            lines.append(f"  (refused) {blocked}")
        for entry in listing.get("schemas") or []:
            lines.append(
                f"  schema {entry.get('name', '?')}: {entry.get('tokens', 0)} token(s)"
            )
        lines.append(
            f"  declared: {bool(receipt.get('declared'))} "
            f"enforced: {bool(receipt.get('enforced'))} - {receipt.get('reason', '')}"
        )
        if listing.get("prompts_error"):
            lines.append(f"  prompts: {listing['prompts_error']}")
        elif not listing.get("prompts"):
            lines.append("  prompts: this server publishes none")
        for row in listing.get("prompts") or []:
            arguments = (
                " " + " ".join(row.get("arguments") or [])
                if row.get("arguments")
                else ""
            )
            lines.append(
                f"  {row.get('command', '?')}{arguments}  {row.get('description', '')}".rstrip()
            )
        budget = listing.get("budget") or {}
        if budget:
            lines.append(
                f"  budget: {budget.get('tools_exposed', 0)} tool(s) exposed, "
                f"{budget.get('projected_context_tokens', 0)} projected token(s), "
                f"{budget.get('calls_made', 0)}/{budget.get('max_calls', 0)} call(s) used"
            )
        deferral = listing.get("deferral") or {}
        if deferral.get("eager_tokens") is not None:
            lines.append(
                f"  schemas: deferred · {deferral.get('eager_tokens', 0)} eager vs "
                f"{deferral.get('deferred_tokens', 0)} deferred token(s) "
                f"({deferral.get('saving_ratio_no_request', 0.0):.0%} saved, "
                f"divisor {deferral.get('chars_per_token', 4)}) · source "
                f"{listing.get('schema_source', '?')}"
            )
        return lines

    servers = list_servers(repo_path)
    if not servers:
        return ["no MCP servers configured"]
    lines: List[str] = []
    problem = connector_state_problem(repo_path)
    if problem:
        lines.append(f"connector state unreadable: {problem}")
    for row in servers:
        flag = (
            " (disabled)" if connector_disabled(row.get("label", ""), repo_path) else ""
        )
        lines.append(
            f"  {row.get('label', '?')}{flag}  [{row.get('source', '?')}]"
            f"  {row.get('command', '')}".rstrip()
        )
        # ``permissions`` arrives as a JSON string because the CLI surface
        # publishes it that way. It is rendered here as PLAIN FIELDS: a receipt
        # a person reads must not carry markup delimiters into a parser, and
        # parsing it back here is the one place the two vocabularies meet.
        summary = _permission_summary(row.get("permissions"))
        lines.append(
            f"      declared: {row.get('permissions_declared')}  {summary}".rstrip()
        )
    return lines


def _permission_summary(raw: Any) -> str:
    """Render one declaration as PLAIN text from ``list_servers``' JSON field.

    Never raises and never emits a bracket: a broken or absent field degrades to
    ``(no declaration)``, which is the honest reading of a declaration this
    surface could not read.
    """
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:
        return "(declaration unreadable)"
    if not isinstance(data, dict) or not data:
        return "(no declaration)"
    tools = data.get("tools") or []
    tool_text = (
        "any" if tools == ["*"] else (", ".join(str(t) for t in tools) or "none")
    )
    write = data.get("write")
    return (
        f"tools: {tool_text} · side_effect<={data.get('side_effect', '-')} · "
        f"write: {'undeclared' if write is None else write} · "
        f"network: {', '.join(data.get('network') or []) or 'none'} · "
        f"pinned: {data.get('pinned_tools', 0)}"
    )


def _health_lines(
    repo_path: Optional[str] = None, label: str = ""
) -> Tuple[List[str], bool]:
    """Return ``(plain lines, ok)`` for ``/mcp health``.

    A connector that hung during the probe is reported with the reason that
    disabled it, because "FAILED" with no reason is exactly the thing an
    operator cannot act on.
    """
    results = check_health(repo_path=repo_path)
    if label:
        results = [row for row in results if row.get("label") == label]
        if not results:
            return [f"no MCP connector named {label!r} is configured"], False
    if not results:
        return ["no MCP servers configured"], True
    lines: List[str] = []
    healthy = 0
    for row in results:
        name = str(row.get("label", "?"))
        ok = bool(row.get("ok"))
        healthy += 1 if ok else 0
        disabled = connector_disabled(name, repo_path)
        tools = row.get("tools") or []
        detail = (
            f"{len(tools)} tool(s)" if ok else str(row.get("error") or "unknown error")
        )
        suffix = ""
        if disabled:
            suffix = "  (disabled)"
        elif row.get("auto_disabled_reason"):
            suffix = f"  ({row['auto_disabled_reason']})"
        lines.append(f"  {name}: {'ok' if ok else 'FAILED'}  {detail}{suffix}")
        for entry in row.get("prompts") or []:
            lines.append(f"      {entry.get('command', '?')}")
    lines.insert(0, f"{healthy}/{len(results)} connector(s) healthy")
    return lines, healthy == len(results)


def mcp_command(
    verb: str,
    argument: str = "",
    *,
    repo_path: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    cwd: Optional[str] = None,
    timeout_s: float = 60.0,
    tier: str = "project",
) -> Dict[str, Any]:
    """Run ONE ``/mcp`` verb and return ``{ok, verb, lines, payload}``.

    This is the primary implementation of the nine verbs, and the ``neo mcp``
    argparse surface delegates here rather than restating the behaviour: one
    implementation of one behaviour, so a fix lands in both doors at once.

    Every verb ACTS and RETURNS. A verb never opens a dialog - the registry
    types the no-argument ``list`` case as the only one whose result may be a
    browser, and that browser is a SHELL concern: these are the lines a shell
    renders, and a shell may mount them in a screen.

    ``lines`` is ALWAYS a non-empty ``list[str]``. A verb with nothing to say
    says so in a line rather than returning an empty list, because an empty
    render is indistinguishable from a verb that does not exist.

    Never raises: a refused verb returns ``ok=False`` with the reason in
    ``lines``, so a hostile argument degrades to one honest sentence.
    """
    name = str(verb or "list").strip().lower()
    argument = str(argument or "").strip()

    def done(
        ok: bool, lines: List[str], payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        body = [str(line) for line in lines if str(line)]
        return {
            "ok": bool(ok),
            "verb": name,
            "lines": body or ["(nothing to report)"],
            "payload": dict(payload or {}),
        }

    if name not in MCP_VERBS:
        return done(
            False, [f"unknown /mcp verb {name!r}; expected one of {list(MCP_VERBS)}"]
        )

    if name == "list":
        try:
            lines = _render_mcp_lines(repo_path=repo_path, label=argument)
        except Exception as exc:
            return done(False, [f"list failed: {type(exc).__name__}: {exc}"])
        return done(True, lines, {"label": argument, "count": len(lines)})

    if name == "add":
        parts = argument.split(None, 1)
        if len(parts) < 2:
            return done(False, ["usage: /mcp add <label> <command...>"])
        try:
            label = add_server(parts[0], parts[1], tier=tier, repo_path=repo_path)
        except Exception as exc:
            return done(False, [f"add failed: {exc}"])
        permission = connector_permissions(label, repo_path=repo_path)
        declared = permission.to_dict() if permission else {}
        return done(
            True,
            [
                f"added connector {label}",
                f"  declared: {permission is not None}"
                f"  tools: {', '.join(declared.get('tools') or []) or '(none declared)'}"
                f"  side_effect<={declared.get('side_effect', '-')}"
                f"  write: {'declared' if declared.get('write') is not None else 'undeclared'}",
            ],
            {"label": label, "declared": permission is not None},
        )

    if name == "remove":
        if not argument:
            return done(False, ["usage: /mcp remove <label>"])
        try:
            remove_server(argument, tier=tier, repo_path=repo_path)
        except Exception as exc:
            return done(False, [f"remove failed: {exc}"])
        return done(True, [f"removed connector {argument}"], {"label": argument})

    if name == "health":
        try:
            lines, ok = _health_lines(repo_path=repo_path, label=argument)
        except Exception as exc:
            return done(False, [f"health probe failed: {type(exc).__name__}: {exc}"])
        return done(ok, lines, {"label": argument, "ok": ok})

    if name == "call":
        parts = argument.split()
        if len(parts) < 2:
            return done(False, ["usage: /mcp call <label> <tool> [args-json]"])
        tool_args: Optional[Dict[str, Any]] = None
        if len(parts) > 2:
            try:
                loaded = json.loads(" ".join(parts[2:]))
            except Exception as exc:
                return done(False, [f"args must be JSON: {exc}"])
            if not isinstance(loaded, dict):
                return done(False, ["tool arguments must be a JSON object"])
            tool_args = loaded
        result = call_tool(
            parts[0],
            parts[1],
            args=tool_args,
            repo_path=repo_path,
            cwd=cwd,
            timeout_s=timeout_s,
            config=config,
        )
        receipt = result.get("receipt") or {}
        lines = [str(result.get("text") or result.get("error") or "(no result)")]
        lines.append(
            f"  {result.get('namespaced') or parts[1]}: "
            f"declared={bool(receipt.get('declared'))} "
            f"enforced={bool(receipt.get('enforced'))} - {receipt.get('reason', '')}"
        )
        if receipt.get("pin_status"):
            lines.append(f"  pin: {receipt['pin_status']}")
        budget = receipt.get("budget") or {}
        if budget:
            lines.append(
                f"  budget: {budget.get('calls_made', 0)}/{budget.get('max_calls', 0)} "
                f"call(s), {budget.get('projected_context_tokens', 0)} projected token(s)"
            )
        return done(bool(result.get("ok")), lines, result)

    if name == "pin":
        parts = argument.split()
        if len(parts) < 3:
            return done(False, ["usage: /mcp pin <label> <tool> <digest>"])
        try:
            result = pin_tool(
                parts[0], parts[1], parts[2], tier=tier, repo_path=repo_path
            )
        except Exception as exc:
            return done(False, [f"pin failed: {exc}"])
        return done(
            True,
            [
                f"{'re-pinned' if result.get('replaced') else 'pinned'} "
                f"{result['namespaced_name']} at {result['definition_digest'][:12]} "
                f"({result['pinned_tools']} pinned)",
                f"  written to {result['path']}",
            ],
            result,
        )

    if name == "reconnect":
        if not argument:
            return done(False, ["usage: /mcp reconnect <label>"])
        result = raw_catalog(
            argument, repo_path=repo_path, cwd=cwd, timeout_s=timeout_s
        )
        if not result.get("ok"):
            tail = f"  disabled: {result.get('disabled')}"
            if result.get("auto_disabled_reason"):
                tail += f" - {result['auto_disabled_reason']}"
            return done(
                False,
                [f"{argument}: reconnect failed: {result.get('error')}", tail],
                result,
            )
        return done(
            True,
            [
                f"{argument}: reconnected, {len(result.get('tools') or [])} tool(s), "
                f"{len(result.get('prompts') or [])} prompt(s)",
                f"  command: {result.get('command')}",
                f"  timeout budget: {result.get('timeout_s')}s",
            ],
            result,
        )

    if name == "enable":
        if not argument:
            return done(False, ["usage: /mcp enable <label>"])
        try:
            result = set_connector_enabled(
                argument, True, repo_path=repo_path, reason="re-enabled via /mcp enable"
            )
        except Exception as exc:
            return done(False, [f"enable failed: {exc}"])
        return done(
            True, [f"enabled connector {argument} (state: {result['path']})"], result
        )

    # ``name == "disable"`` - the last declared verb, so the fallthrough is it.
    target = argument.strip()
    if not target:
        return done(False, ["usage: /mcp disable <label>|all"])
    if target.casefold() == "all":
        try:
            result = disable_all_connectors(
                repo_path=repo_path, reason="disabled via /mcp disable all"
            )
        except Exception as exc:
            return done(False, [f"disable failed: {exc}"])
        return done(
            True,
            [
                "disabled every connector (declarations and permissions untouched)",
                f"  state: {result['path']}",
                "  restore one with: /mcp enable <label>",
            ],
            result,
        )
    try:
        result = set_connector_enabled(
            target, False, repo_path=repo_path, reason="disabled via /mcp disable"
        )
    except Exception as exc:
        return done(False, [f"disable failed: {exc}"])
    return done(
        True,
        [
            f"disabled connector {target} (declaration and permissions untouched)",
            f"  state: {result['path']}",
        ],
        result,
    )
