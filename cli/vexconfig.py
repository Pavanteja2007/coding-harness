"""Vex user config file (~/.vex/config.toml) — Task C of the CLI
session-persistence pass.

Precedence (low -> high): harness DEFAULTS < ~/.vex/config.toml (or the
file named by VEX_CONFIG) < CLI flags / session state. Flags win by
construction: the config file only fills keys the user did NOT pass
explicitly — `--budget 5` beats `budget_cap_usd = 1.0` in the file.

Schema (all optional; unknown keys are KEPT — harness.get_config passes
them through, so future/other-terminal knobs work without this module
needing changes):

    # ~/.vex/config.toml
    model = "z-ai/glm-5.3-free"        # preferred model (pin; omit = router)
    provider = "openai"                # litellm provider
    budget_cap_usd = 2.0               # per-task cost cap
    max_retries = 3                    # full attempts per task
    plan_preview = true                # confirm plan before edits (Task D)
    log_verbosity = "normal"           # normal | quiet  (quiet hides the
                                       # per-event spinner labels, keeps the
                                       # summary)

TOML parse errors NEVER crash the CLI: a broken file is reported once on
stderr and ignored (defaults apply) — a config file is a convenience,
not a load-bearing input.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_DIR = ".vex"
CONFIG_NAME = "config.toml"

# Keys this module itself interprets (anything else passes through to the
# harness/router config merge untouched).
_VEX_KEYS = {
    "model",
    "provider",
    "budget_cap_usd",
    "max_retries",
    "plan_preview",
    "log_verbosity",
}


def config_path() -> Path:
    """The effective config file path: $VEX_CONFIG wins, else ~/.vex/config.toml."""
    env = os.environ.get("VEX_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / CONFIG_DIR / CONFIG_NAME


def load_vex_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the Vex config file into a flat dict; {} when absent/unreadable.

    Assumes: a missing file is the NORMAL case (fresh install); a present
    but unparseable file is reported to stderr once and skipped, never
    raised — the CLI must stay usable with a broken config. Values are
    shallow-validated (types for the keys this module itself interprets);
    a wrong-typed value is dropped with the same one-shot warning.
    """
    p = path if path is not None else config_path()
    if not p.is_file():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        _warn(f"cannot read {p}: {exc} — ignoring config file")
        return {}
    data = _parse_toml(text)
    if data is None:
        _warn(f"{p} is not valid TOML — ignoring config file")
        return {}
    if not isinstance(data, dict):
        _warn(f"{p}: top level must be a table — ignoring config file")
        return {}

    out: Dict[str, Any] = {}
    # one nested table is allowed for grouping: [vex] ...
    if "vex" in data and isinstance(data["vex"], dict):
        merged = dict(data["vex"])
        merged.update({k: v for k, v in data.items() if k != "vex"})
        data = merged

    for key, value in data.items():
        if key in _VEX_KEYS and not _type_ok(key, value):
            _warn(f"{p}: {key} has wrong type ({type(value).__name__}) — dropping it")
            continue
        out[key] = value
    return out


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
    if key in ("model", "provider", "log_verbosity"):
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


def apply_config_defaults(
    config: Dict[str, Any],
    file_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge file defaults UNDER an existing config dict (Task C).

    Assumes `config` holds EXPLICIT settings (CLI flags, session state —
    the winners); keys already present are never overwritten. Returns a
    NEW dict (inputs untouched). This is the whole precedence mechanism:
    the caller builds explicit-first, we fill the gaps from the file.
    """
    fc = file_config if file_config is not None else load_vex_config()
    merged: Dict[str, Any] = dict(fc)
    merged.update(config or {})
    return merged


def sessions_log_root(file_config: Optional[Dict[str, Any]] = None) -> Path:
    """Log root for session persistence (Task A): config `log_root` wins,
    else ./logs relative to CWD (the harness default)."""
    fc = file_config if file_config is not None else load_vex_config()
    lr = fc.get("log_root")
    return Path(lr).expanduser() if isinstance(lr, str) and lr.strip() else Path("logs")
