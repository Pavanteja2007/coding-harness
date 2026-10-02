"""`neo update` — self-update (Task E — CLI citizenship).

Detects how Neo was installed (pipx / dedicated ~/.neo-venv from the
curl installers / plain pip / source tree) and re-runs the matching
upgrade command, so users never have to remember the install method:

    pipx           ->  pipx upgrade neo-agent-cli   (fallback: reinstall)
    ~/.neo-venv    ->  <venv python> -m pip install --upgrade <source>
    pip (any venv) ->  python -m pip install --upgrade <source>
    source tree    ->  honest refusal with the git pull + pip -e . recipe

Version check: `--check` compares the installed version against the
latest release on PyPI first (the installers' and `pip install`'s
source of truth), with the GitHub tags API as fallback — a plain GET
each, no auth, 5s timeout, never a crash on offline/filtered networks
(prints "cannot reach PyPI", exit 4 per the network-error category).

Install source resolution mirrors the installers (install.sh/.ps1):
`NEO_INSTALL_SOURCE` env wins outright, else an explicitly-set
`NEO_INSTALL_REPO`/`NEO_INSTALL_REF` pins a git URL, else the PyPI
distribution name `neo-agent-cli` (the installed COMMAND stays `neo` —
see pyproject.toml's naming note).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional, Tuple

DIST_NAME = "neo-agent-cli"
DEFAULT_REPO = "Pavanteja2007/coding-harness"
DEFAULT_REF = "main"
_VENV_DIR_NAME = ".neo-venv"


def install_source(ref: Optional[str] = None) -> str:
    """The pip requirement to upgrade from (mirrors the installers).

    PyPI (`neo-agent-cli`, the default install path) unless the operator
    pinned a git source: `NEO_INSTALL_SOURCE` wins outright, else an
    explicitly-set `NEO_INSTALL_REPO`/`NEO_INSTALL_REF` builds the
    `git+https://...` URL. Never raises."""
    if os.environ.get("NEO_INSTALL_SOURCE"):
        return os.environ["NEO_INSTALL_SOURCE"]
    repo = os.environ.get("NEO_INSTALL_REPO")
    ref = ref or os.environ.get("NEO_INSTALL_REF")
    if repo or ref:
        return f"git+https://github.com/{repo or DEFAULT_REPO}.git@{ref or DEFAULT_REF}"
    return DIST_NAME


def installed_version() -> str:
    """The currently installed version ('' when unknown — never raises)."""
    try:
        from cli.main import _get_version

        v = _get_version()
        return "" if v.endswith("+source") else v
    except Exception:
        return ""


def _parse_release_version(value: str) -> Any:
    """Parse one release version using PEP 440 rules and reject bad input."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("version must be a non-empty string")
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError as exc:
        raise RuntimeError("packaging is required for release version checks") from exc
    normalized = value.strip()
    if normalized[:1].lower() == "v":
        normalized = normalized[1:]
        if normalized[:1].lower() == "v":
            raise ValueError("version may have at most one leading v")
    try:
        return Version(normalized)
    except InvalidVersion as exc:
        raise ValueError(f"invalid version: {value!r}") from exc


def _canonical_release_version(value: str) -> str:
    """Return canonical text for a valid PEP 440 release version."""
    return str(_parse_release_version(value))


def _is_stable_release(version: Any) -> bool:
    """Return whether a parsed version is a final or post release."""
    return not version.is_prerelease and not version.is_devrelease


def _latest_from_tags(tags: Any) -> Optional[str]:
    """Return the highest valid stable version from a GitHub tag response."""
    if not isinstance(tags, (list, tuple)):
        return None
    latest: Any = None
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        name = tag.get("name")
        if not isinstance(name, str):
            continue
        try:
            candidate = _parse_release_version(name)
        except (ValueError, RuntimeError):
            continue
        if _is_stable_release(candidate) and (latest is None or candidate > latest):
            latest = candidate
    return str(latest) if latest is not None else None


def _latest_from_github(timeout_s: float) -> Optional[str]:
    """Latest stable release from paginated GitHub tags, or None."""
    repo = os.environ.get("NEO_INSTALL_REPO", DEFAULT_REPO)
    collected: list = []
    for page in range(1, 11):
        url = f"https://api.github.com/repos/{repo}/tags?per_page=100&page={page}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "neo-update-check",
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                tags = json.loads(resp.read().decode("utf-8"))
        except Exception:
            break
        if not isinstance(tags, list) or not tags:
            break
        collected.extend(tags)
        if len(tags) < 100:
            break
    return _latest_from_tags(collected)


def _latest_from_pypi(timeout_s: float) -> Optional[str]:
    """Latest valid version on PyPI, or None when unavailable or malformed."""
    try:
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{DIST_NAME}/json",
            headers={"User-Agent": "neo-update-check"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        info = data.get("info") if isinstance(data, dict) else None
        value = info.get("version") if isinstance(info, dict) else None
        if not isinstance(value, str):
            return None
        candidate = _parse_release_version(value)
        if not _is_stable_release(candidate):
            return None
        return str(candidate)
    except (ValueError, RuntimeError):
        return None
    except Exception:
        return None


def latest_available_version(timeout_s: float = 5.0) -> Optional[str]:
    """Return the newest valid released version, or None when unavailable.

    Tries the PyPI JSON API first, then the GitHub tags. Invalid values are
    never returned; PyPI falls back to GitHub when its response is unusable.
    """
    v = _latest_from_pypi(timeout_s)
    if v is not None:
        try:
            return _canonical_release_version(v)
        except (ValueError, RuntimeError):
            pass
    v = _latest_from_github(timeout_s)
    if v is None:
        return None
    try:
        return _canonical_release_version(v)
    except (ValueError, RuntimeError):
        return None


# Backwards-compatible alias (the original name this module shipped).
latest_github_version = latest_available_version


def detect_install_method() -> Tuple[str, str]:
    """(method, description) for how THIS running neo was installed.

    Methods: 'pipx' | 'venv' (the installer's dedicated ~/.neo-venv) |
    'pip' (any other Python env) | 'source'.
    """
    exe = Path(sys.executable).resolve()
    parts = [part.casefold() for part in exe.parts]
    if "venvs" in parts:
        venvs_index = parts.index("venvs")
        if (
            venvs_index + 1 < len(parts)
            and parts[venvs_index + 1] == DIST_NAME.casefold()
        ):
            return "pipx", "pipx (isolated per-app venv)"
    if _VENV_DIR_NAME in parts:
        return "venv", "the dedicated installer venv (~/.neo-venv)"
    # Source tree: we're running from a checkout whose cli/ sits next
    # to the git metadata of THIS project.
    try:
        cli_dir = Path(__file__).resolve().parent
        if (cli_dir.parent / ".git").exists():
            return "source", "a source checkout (git clone)"
    except OSError:
        pass
    return "pip", "a pip-installed environment"


def _run(cmd: list, **kw) -> int:
    """subprocess.run with console passthrough (the user sees pip's
    own progress/errors — neo must not hide the tool doing the work)."""
    return subprocess.run(cmd, **kw).returncode


def cmd_update(args: Any) -> int:
    """`neo update` — upgrade in place; `--check` only reports."""
    from cli import ui

    con = ui.console()
    err = ui.err_console()
    method, desc = detect_install_method()
    current = installed_version()

    if getattr(args, "check", False):
        con.print(f"[neo.muted]installed:  {current or '(unknown)'}[/]")
        try:
            current_version = _parse_release_version(current)
        except (ValueError, RuntimeError) as exc:
            err.print(f"[neo.error]invalid installed version: {exc}[/]")
            return 1
        latest = latest_available_version()
        if latest is None:
            err.print(
                "[neo.error]cannot reach PyPI or GitHub to fetch the latest "
                "release (offline or blocked?)[/]"
            )
            return 4  # network error category (cli.exit_codes)
        try:
            latest_version = _parse_release_version(latest)
        except (ValueError, RuntimeError) as exc:
            err.print(f"[neo.error]invalid latest release version: {exc}[/]")
            return 1
        con.print(f"[neo.muted]latest:     {latest}[/]")
        if current_version == latest_version:
            con.print(f"[neo.ok]up to date ({latest})[/]")
            return 0
        if current_version > latest_version:
            con.print(
                f"[neo.ok]up to date (installed {current} is newer than {latest})[/]"
            )
            return 0
        con.print("[neo.warn]update available[/]")
        return 0

    src = install_source()

    if method == "source":
        err.print(
            "[neo.warn]this neo is running from a source checkout — "
            "self-update is a git pull, not a package upgrade[/]"
        )
        con.print("[neo.muted]to update:[/]")
        con.print("[neo.muted]  git pull[/]")
        con.print(f"[neo.muted]  {sys.executable} -m pip install -e .[/]")
        return 1

    con.print(f"[neo.muted]installed via {desc}[/]")
    con.print(f"[neo.muted]upgrading from {src}[/]")
    try:
        if method == "pipx":
            con.print(f"[neo.muted]running: pipx upgrade {DIST_NAME}[/]")
            rc = _run([shutil.which("pipx") or "pipx", "upgrade", DIST_NAME])
            if rc != 0:
                # not-installed-by-pipx edge / metadata weirdness: force
                con.print(
                    "[neo.muted]pipx upgrade failed - reinstalling from source[/]"
                )
                rc = _run([shutil.which("pipx") or "pipx", "install", "--force", src])
        elif method == "venv":
            con.print(
                f"[neo.muted]running: {sys.executable} -m pip install "
                f"--upgrade {src}[/]"
            )
            rc = _run([sys.executable, "-m", "pip", "install", "--upgrade", src])
        else:
            con.print(
                f"[neo.muted]running: {sys.executable} -m pip install "
                f"--upgrade {src}[/]"
            )
            rc = _run([sys.executable, "-m", "pip", "install", "--upgrade", src])
    except OSError as exc:
        err.print(f"[neo.error]error: could not launch the installer: {exc}[/]")
        return 3  # environment error: the local toolchain is broken

    if rc != 0:
        err.print(f"[neo.error]upgrade command failed (exit {rc})[/]")
        return 1
    new = installed_version()
    if new and current:
        try:
            before_version = _parse_release_version(current)
            new_version = _parse_release_version(new)
        except (ValueError, RuntimeError):
            con.print(
                f"[neo.ok]upgrade command finished[/] [neo.muted](installed version: {new})[/]"
            )
        else:
            if new_version < before_version:
                err.print(
                    f"[neo.error]upgrade command selected a downgrade: {current} -> {new}[/]"
                )
                return 1
            if new_version > before_version:
                con.print(f"[neo.ok]updated to {new}[/]")
            else:
                con.print(f"[neo.ok]already at {new}[/]")
    elif new:
        con.print(
            f"[neo.ok]upgrade command finished[/] [neo.muted](installed version: {new})[/]"
        )
    else:
        con.print("[neo.ok]upgrade command finished[/]")
    con.print("[neo.muted]verify: neo --version[/]")
    return 0


def add_update_parser(sub: Any) -> None:
    """Wire the `update` subcommand onto the CLI's subparsers."""
    p = sub.add_parser(
        "update",
        help="self-update Neo (or --check: report the latest release)",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="only report installed vs latest version (no upgrade)",
    )
    p.set_defaults(func=cmd_update)
