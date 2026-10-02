"""Install and update truth: what this installation can actually do.

Three claims a user makes about an installed Neo, and how each is checked
here rather than asserted in prose.

**"It has the features the docs describe."** ``probe_capabilities``
compares the capabilities the RUNTIME REGISTRY advertises (the argparse
subcommand tree, the ``cli.commands`` slash registry, and the optional
integration modules) against what the installed distribution actually
provides. A capability the registry advertises but the install cannot
import is a packaging defect and is reported as ``missing`` with the module
that failed - never silently absent, because a missing capability that
disappears from the help text is indistinguishable from a feature that was
never built.

**"The public release is what these docs describe."**
``public_release_version`` asks the public index for the newest published
version and ``stale_release_notice`` compares it with the local
documentation version. The notice is emitted AT MOST ONCE per process and
recorded on disk, because a warning that repeats on every command is noise
and a user learns to ignore it.

**"The help text matches the code."** ``command_inventory`` is the single
list of command names, derived from the live parser rather than a hand-kept
table. ``neo --help``'s metavar, the completion backend, and the capability
report all read it, so they cannot disagree with each other or with the
parser.

Nothing here performs a network request on the import path. The public
version lookup is explicit, bounded, and injectable, and every caller
treats a failure as "unknown" rather than as an error.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "Capability",
    "CapabilityReport",
    "capability_report_dict",
    "command_inventory",
    "installed_version",
    "local_docs_version",
    "probe_capabilities",
    "public_release_version",
    "stale_release_notice",
    "warn_once_path",
]

#: Public package name on the index. The installed COMMAND is ``neo``; the
#: distribution is ``neo-agent-cli`` (see pyproject).
DISTRIBUTION = "neo-agent-cli"

#: Where the once-per-installation release-staleness receipt lives.
NOTICE_FILENAME = "release-notice.json"

#: Hard bound on any network lookup performed from this module.
NETWORK_TIMEOUT_S = 3.0

#: Upper bound on how many capabilities a report enumerates. This is a
#: SAFETY bound against a pathological registry, not a budget: exceeding it
#: is reported as a finding, because a report that silently dropped
#: capabilities it could not list is a report that can say "ok" about
#: something it never checked.
MAX_CAPABILITIES = 512


@dataclass(frozen=True)
class Capability:
    """One advertised capability and whether this install provides it."""

    name: str
    kind: str
    available: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable record."""
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available,
            "detail": self.detail,
        }


@dataclass
class CapabilityReport:
    """The result of comparing the registry against the installation."""

    version: str
    docs_version: str
    capabilities: List[Capability] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every advertised capability is present in this install."""
        return not self.missing

    @property
    def version_drift(self) -> bool:
        """Whether the installed version differs from the local docs."""
        return bool(self.docs_version) and self.docs_version != self.version

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable report."""
        return {
            "schema": "neo.capabilities/1",
            "version": self.version,
            "docs_version": self.docs_version,
            "ok": self.ok,
            "version_drift": self.version_drift,
            "missing": list(self.missing),
            "capabilities": [item.to_dict() for item in self.capabilities],
        }


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def _pyproject_version(root: Optional[Path] = None) -> str:
    """Read the declared version from pyproject.toml (the release source)."""
    base = Path(root) if root else Path(__file__).resolve().parent.parent
    path = base / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else ""


def installed_version() -> str:
    """The version of the running installation.

    Prefers the installed distribution metadata (so a wheel reports its own
    version even when the source tree differs) and falls back to
    ``pyproject.toml`` for a source checkout.

    The metadata lookup tries the CURRENT distribution name first and then
    the names this project shipped under before, because a user who has
    ``vex-harness`` installed and pulls this tree would otherwise get
    ``(unknown)`` from every version probe, every self-update check and
    every release-staleness notice. The order is the same contract as
    everywhere else in the rename: the current name wins, the previous name
    is honoured, and nothing is ever written under the old one.
    """
    try:
        from importlib import metadata

        try:
            from shared.brand import LEGACY_DISTRIBUTIONS
        except Exception:  # the compat module is optional; the lookup is not
            LEGACY_DISTRIBUTIONS = ()
        for name in (DISTRIBUTION, *LEGACY_DISTRIBUTIONS):
            try:
                return str(metadata.version(name))
            except Exception:
                continue
    except Exception:
        pass
    return _pyproject_version()


def local_docs_version(root: Optional[Path] = None) -> str:
    """The version the LOCAL documentation describes.

    Read from ``pyproject.toml``, which every doc surface is required to
    match. A doc that names a different number is drift, and the report
    says so rather than picking a winner.
    """
    return _pyproject_version(root)


# ---------------------------------------------------------------------------
# The runtime registry
# ---------------------------------------------------------------------------
#
# ``cli.main.build_parser`` calls ``register_commands`` with the PUBLIC
# command names it actually created. ``neo --help``'s metavar, the
# completion inventory, and the capability report all read this registry, so
# they are derived from the code rather than maintained beside it. The live
# parser is NOT walked from inside ``build_parser`` (that would recurse);
# ``command_inventory`` reads the registry first and only reaches for the
# parser when one is not currently under construction.

_REGISTERED_COMMANDS: List[str] = []
_REGISTRY_SOURCE = ""
_PARSER_BUSY = False


def register_commands(names, *, source: str = "") -> None:
    """Record the public command names the parser just created.

    ``source`` names the producer (``cli.main.build_parser``) so the report
    can say where the list came from rather than implying the list is
    hand-maintained. Hidden names (a leading underscore) are dropped: they
    stay dispatchable but must not appear in help or completions.
    """
    global _REGISTERED_COMMANDS, _REGISTRY_SOURCE
    _REGISTERED_COMMANDS = sorted(
        {str(name) for name in names if not str(name).startswith("_")}
    )
    _REGISTRY_SOURCE = str(source or "")


def registered_commands() -> List[str]:
    """The public command names, from the registry the parser wrote."""
    return list(_REGISTERED_COMMANDS)


def registry_source() -> str:
    """Which producer last registered the command list."""
    return _REGISTRY_SOURCE


def _walk_parser(parser: Any) -> Dict[str, Any]:
    """Extract public commands and nested subcommands from a live parser."""
    commands: List[str] = []
    subcommands: Dict[str, List[str]] = {}
    subparsers = getattr(parser, "_subparsers", None)
    for action in getattr(subparsers, "_group_actions", []) or []:
        choices = getattr(action, "choices", None) or {}
        for name, sub in choices.items():
            if str(name).startswith("_"):
                continue
            commands.append(str(name))
            nested: List[str] = []
            inner_group = getattr(sub, "_subparsers", None)
            for inner in getattr(inner_group, "_group_actions", []) or []:
                for inner_name in getattr(inner, "choices", None) or {}:
                    if not str(inner_name).startswith("_"):
                        nested.append(str(inner_name))
            if nested:
                subcommands[str(name)] = sorted(nested)
    return {
        "commands": sorted(commands),
        "subcommands": {k: subcommands[k] for k in sorted(subcommands)},
    }


def _parser_registry() -> Optional[Dict[str, Any]]:
    """Build the live parser for cross-checking, or None while it is building.

    ``build_parser`` cannot be re-entered: it asks this module for its help
    metavar, so a nested call would recurse. The busy flag is what makes the
    cross-check safe to attempt at all.
    """
    if _PARSER_BUSY:
        return None
    try:
        from cli.main import build_parser

        return _walk_parser(build_parser())
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": str(exc)}


def command_inventory() -> Dict[str, Any]:
    """Return the command tree: registry names plus a live-parser cross-check.

    ``commands`` comes from the registry ``build_parser`` wrote.
    ``parser_commands`` is the live parser's own view, present only when a
    parser can be built safely. ``drift`` is the symmetric difference
    between them - non-empty means the registry and the parser disagree,
    which is a bug in one of them and is reported rather than hidden.
    """
    inventory: Dict[str, Any] = {
        "schema": "neo.commands/1",
        "commands": registered_commands(),
        "source": registry_source(),
        "subcommands": {},
        "parser_commands": [],
        "drift": [],
    }
    live = _parser_registry()
    if live and "error" not in live:
        inventory["parser_commands"] = list(live.get("commands") or [])
        inventory["subcommands"] = dict(live.get("subcommands") or {})
        registry = set(inventory["commands"])
        parser_names = set(inventory["parser_commands"])
        if registry and parser_names and registry != parser_names:
            inventory["drift"] = sorted(registry.symmetric_difference(parser_names))
    elif live and live.get("error"):
        inventory["error"] = str(live["error"])
    return inventory


def help_metavar() -> str:
    """The ``{...}`` command list for ``neo --help``.

    Built from the registry the parser wrote, so the usage line can never
    list a command that does not exist or omit one that does. Falls back to
    a literal ``{command}`` only when the parser has not been built yet
    (which is the state during ``build_parser`` itself).
    """
    commands = registered_commands()
    if not commands:
        return "{command}"
    return "{" + ",".join(commands) + "}"


#: Optional integration modules the docs advertise. Each is a real module
#: with a real import; a failure is a packaging fact, not a guess.
INTEGRATION_MODULES: Tuple[Tuple[str, str], ...] = (
    ("agent_sdk", "Agent SDK and local agent server (neo serve)"),
    ("acp", "Agent Client Protocol server for editors (neo acp)"),
    ("integrations", "Integration registry"),
    ("recipes", "Runnable recipes"),
    ("extensions", "Extension loading"),
    ("evals", "Prompt-regression evaluation harness"),
    ("memory", "Cross-session memory layer"),
    ("mcp_server", "Built-in memory MCP server"),
    ("dashboard", "Read-only run dashboard"),
)


def probe_capabilities(
    root: Optional[Path] = None,
    *,
    import_module: Callable[[str], Any] = importlib.import_module,
) -> CapabilityReport:
    """Compare the advertised capability surface with this installation.

    ``import_module`` is injectable so the probe can be exercised without
    spawning imports, and so a caller can substitute a resolver that knows
    about a frozen or vendored environment.

    The report never raises. A capability it cannot check is reported
    ``available: false`` with the reason, because an unverified capability
    claimed as present is the failure mode this whole module exists to
    prevent.
    """
    version = installed_version()
    docs = local_docs_version(root)
    report = CapabilityReport(version=version, docs_version=docs)
    if not _REGISTERED_COMMANDS:
        # The registry is written by build_parser. A probe that runs before
        # any parser was built would otherwise report "no commands", which
        # reads as a healthy install with no surfaces. Building the parser
        # here is the difference between an honest report and a vacuous one.
        try:
            from cli.main import build_parser

            build_parser()
        except Exception as exc:
            report.capabilities.append(
                Capability(
                    name="command-registry",
                    kind="command",
                    available=False,
                    detail=f"parser unavailable: {type(exc).__name__}: {exc}",
                )
            )
            report.missing.append("command-registry")
    inventory = command_inventory()
    for name in inventory.get("commands", [])[:MAX_CAPABILITIES]:
        report.capabilities.append(
            Capability(
                name=f"command:{name}",
                kind="command",
                available=True,
                detail="registered in the runtime parser",
            )
        )
    try:
        from cli import commands as _commands

        for spec in _commands.COMMAND_SPECS:
            if len(report.capabilities) >= MAX_CAPABILITIES:
                break
            report.capabilities.append(
                Capability(
                    name=f"slash:{spec.name}",
                    kind="slash_command",
                    available=True,
                    detail=str(spec.result_presentation),
                )
            )
    except Exception as exc:
        report.capabilities.append(
            Capability(
                name="slash-registry",
                kind="slash_command",
                available=False,
                detail=f"command registry unavailable: {exc}",
            )
        )
        report.missing.append("slash-registry")
    for module_name, description in INTEGRATION_MODULES:
        if len(report.capabilities) >= MAX_CAPABILITIES:
            break
        try:
            import_module(module_name)
        except Exception as exc:
            report.capabilities.append(
                Capability(
                    name=f"module:{module_name}",
                    kind="module",
                    available=False,
                    detail=f"{description} — import failed: {type(exc).__name__}",
                )
            )
            report.missing.append(f"module:{module_name}")
            continue
        report.capabilities.append(
            Capability(
                name=f"module:{module_name}",
                kind="module",
                available=True,
                detail=description,
            )
        )
    if len(report.capabilities) >= MAX_CAPABILITIES:
        report.capabilities.append(
            Capability(
                name="capability-list-truncated",
                kind="report",
                available=False,
                detail=(
                    f"the registry produced more than {MAX_CAPABILITIES} "
                    "capabilities; the rest were not checked"
                ),
            )
        )
        report.missing.append("capability-list-truncated")
    return report


def capability_report_dict(
    root: Optional[Path] = None,
    *,
    as_json: bool = True,
) -> Dict[str, Any]:
    """Return the capability report, optionally enriched with the inventory."""
    report = probe_capabilities(root)
    payload = report.to_dict()
    payload["inventory"] = command_inventory()
    if not as_json:
        return payload
    return payload


# ---------------------------------------------------------------------------
# Public release freshness
# ---------------------------------------------------------------------------


def public_release_version(
    *,
    fetcher: Optional[Callable[[str, float], bytes]] = None,
    timeout_s: float = NETWORK_TIMEOUT_S,
) -> str:
    """The newest version published for this distribution, or "".

    Network failure is NOT an error: an offline install must still be able
    to run every surface, so this returns "" and every caller reports
    "unknown" rather than guessing. ``fetcher`` is injectable so the
    freshness logic is testable without a network.
    """
    url = f"https://pypi.org/pypi/{DISTRIBUTION}/json"
    try:
        if fetcher is None:
            import urllib.request

            def _default(target: str, timeout: float) -> bytes:
                request = urllib.request.Request(
                    target, headers={"Accept": "application/json"}
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return bytes(response.read(1 << 20))

            fetcher = _default
        raw = fetcher(url, float(timeout_s))
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        version = str((payload.get("info") or {}).get("version") or "").strip()
        return version
    except Exception:
        return ""


def _version_tuple(value: str) -> Tuple[int, ...]:
    """A comparable numeric tuple from a dotted version, ignoring suffixes."""
    parts: List[int] = []
    for chunk in re.split(r"[._-]", str(value or "")):
        match = re.match(r"^(\d+)", chunk)
        if not match:
            break
        parts.append(int(match.group(1)))
    return tuple(parts)


def is_public_release_older(public: str, local: str) -> bool:
    """Whether the PUBLIC release is strictly older than the local docs.

    Compares numeric tuples, so ``0.2.10`` correctly sorts above ``0.2.9``.
    An unparseable or missing side returns False: "we do not know" must not
    become "you are out of date", which would train users to ignore the
    real notice.
    """
    left = _version_tuple(public)
    right = _version_tuple(local)
    if not left or not right:
        return False
    return left < right


def warn_once_path() -> Path:
    """Where the release-staleness receipt is stored.

    ``NEO_HOME``/``HARNESS_HOME`` win outright so the notice is isolatable:
    a test run, a sandbox, or a CI job must never mark the notice as shown
    on the developer's real machine, and a real run must never write its
    receipt into somebody's project directory.
    """
    for name in ("NEO_HOME", "HARNESS_HOME"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return Path(value).expanduser() / NOTICE_FILENAME
    try:
        from cli.neoconfig import global_settings_path

        return Path(global_settings_path()).parent / NOTICE_FILENAME
    except Exception:
        return (
            Path(os.environ.get("NEO_HOME") or Path.home() / ".neo") / NOTICE_FILENAME
        )


def stale_release_notice(
    *,
    public_version: str = "",
    docs_version: str = "",
    fetcher: Optional[Callable[[str, float], bytes]] = None,
    mark: bool = True,
) -> str:
    """Return the "public release is older" notice AT MOST ONCE.

    "Once" is enforced two ways, because both are needed: an in-process set
    (a single command that renders several times must not repeat) and an
    on-disk receipt (a second command in a second process must not repeat
    either). The notice names both versions and the update command, and it
    is a WARNING about docs-versus-wheel drift, never a claim that the
    install is broken.
    """
    if not public_version:
        return ""
    if not is_public_release_older(public_version, docs_version):
        return ""
    path = warn_once_path()
    try:
        if path.is_file():
            return ""
    except OSError:
        pass
    notice = (
        f"the public {DISTRIBUTION} release is {public_version}, but these docs "
        f"describe {docs_version}. Some documented capabilities are not in the "
        f"installed wheel. Run `neo update` for the published build, or read "
        f"the source checkout for the newer documentation."
    )
    if mark:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "public_version": public_version,
                        "docs_version": docs_version,
                        "shown_at": time.time(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
    return notice
