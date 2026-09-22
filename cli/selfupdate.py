"""`vex update` — self-update (Task E — CLI citizenship).

Detects how Vex was installed (pipx / dedicated ~/.vex-venv from the
curl installers / plain pip / source tree) and re-runs the matching
upgrade command, so users never have to remember the install method:

    pipx           ->  pipx upgrade vex-harness   (fallback: reinstall)
    ~/.vex-venv    ->  <venv python> -m pip install --upgrade <source>
    pip (any venv) ->  python -m pip install --upgrade <source>
    source tree    ->  honest refusal with the git pull + pip -e . recipe

Version check: `--check` compares the installed version against the
latest release on PyPI first (the installers' and `pip install`'s
source of truth), with the GitHub tags API as fallback — a plain GET
each, no auth, 5s timeout, never a crash on offline/filtered networks
(prints "cannot reach PyPI", exit 4 per the network-error category).

Install source resolution mirrors the installers (install.sh/.ps1):
`VEX_INSTALL_SOURCE` env wins outright, else an explicitly-set
`VEX_INSTALL_REPO`/`VEX_INSTALL_REF` pins a git URL, else the PyPI
distribution name `vex-harness` (the installed COMMAND stays `vex` —
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
from typing import Optional, Tuple

DIST_NAME = "vex-harness"
DEFAULT_REPO = "Pavanteja2007/coding-harness"
DEFAULT_REF = "main"
_VENV_DIR_NAME = ".vex-venv"


def install_source(ref: Optional[str] = None) -> str:
    """The pip requirement to upgrade from (mirrors the installers).

    PyPI (`vex-harness`, the default install path) unless the operator
    pinned a git source: `VEX_INSTALL_SOURCE` wins outright, else an
    explicitly-set `VEX_INSTALL_REPO`/`VEX_INSTALL_REF` builds the
    `git+https://...` URL. Never raises."""
    if os.environ.get("VEX_INSTALL_SOURCE"):
        return os.environ["VEX_INSTALL_SOURCE"]
    repo = os.environ.get("VEX_INSTALL_REPO")
    ref = ref or os.environ.get("VEX_INSTALL_REF")
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


def _latest_from_github(timeout_s: float) -> Optional[str]:
    """Latest tag via the GitHub API, or None (never raises)."""
    repo = os.environ.get("VEX_INSTALL_REPO", DEFAULT_REPO)
    url = f"https://api.github.com/repos/{repo}/tags?per_page=10"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "vex-update-check",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        for t in tags:
            name = (t.get("name") or "").lstrip("v")
            if name:
                return name
        return None
    except Exception:
        return None


def _latest_from_pypi(timeout_s: float) -> Optional[str]:
    """Latest published version on PyPI (the dist is vex-harness), or
    None (never raises; also the fallback when the GitHub API is
    unreachable/filtered — PyPI's JSON API is a different edge)."""
    try:
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{DIST_NAME}/json",
            headers={"User-Agent": "vex-update-check"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("info") or {}).get("version") or None
    except Exception:
        return None


def latest_available_version(timeout_s: float = 5.0) -> Optional[str]:
    """The newest released version, or None when no source is reachable.

    Tries the PyPI JSON API (the installers' source of truth) first,
    then the GitHub tags. Never raises.
    """
    v = _latest_from_pypi(timeout_s)
    if v is not None:
        return v
    return _latest_from_github(timeout_s)


# Backwards-compatible alias (the original name this module shipped).
latest_github_version = latest_available_version


def detect_install_method() -> Tuple[str, str]:
    """(method, description) for how THIS running vex was installed.

    Methods: 'pipx' | 'venv' (the installer's dedicated ~/.vex-venv) |
    'pip' (any other Python env) | 'source'.
    """
    exe = Path(sys.executable).resolve()
    exe_s = str(exe).lower()
    if "pipx" in exe_s and "venvs" in exe_s:
        return "pipx", "pipx (isolated per-app venv)"
    if _VENV_DIR_NAME in exe_s:
        return "venv", "the dedicated installer venv (~/.vex-venv)"
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
    own progress/errors — vex must not hide the tool doing the work)."""
    return subprocess.run(cmd, **kw).returncode


def cmd_update(args) -> int:
    """`vex update` — upgrade in place; `--check` only reports."""
    from cli import ui

    con = ui.console()
    err = ui.err_console()
    method, desc = detect_install_method()
    current = installed_version()

    if getattr(args, "check", False):
        con.print(f"[vex.muted]installed:  {current or '(unknown)'}[/]")
        latest = latest_available_version()
        if latest is None:
            err.print(
                "[vex.error]cannot reach PyPI or GitHub to fetch the latest "
                "release (offline or blocked?)[/]"
            )
            return 4  # network error category (cli.exit_codes)
        con.print(f"[vex.muted]latest:     {latest}[/]")
        if current and current == latest:
            con.print(f"[vex.ok]up to date ({latest})[/]")
            return 0
        con.print("[vex.warn]update available[/]")
        return 0

    src = install_source()

    if method == "source":
        err.print(
            "[vex.warn]this vex is running from a source checkout — "
            "self-update is a git pull, not a package upgrade[/]"
        )
        con.print("[vex.muted]to update:[/]")
        con.print("[vex.muted]  git pull[/]")
        con.print(f"[vex.muted]  {sys.executable} -m pip install -e .[/]")
        return 1

    con.print(f"[vex.muted]installed via {desc}[/]")
    con.print(f"[vex.muted]upgrading from {src}[/]")
    try:
        if method == "pipx":
            con.print(f"[vex.muted]running: pipx upgrade {DIST_NAME}[/]")
            rc = _run([shutil.which("pipx") or "pipx", "upgrade", DIST_NAME])
            if rc != 0:
                # not-installed-by-pipx edge / metadata weirdness: force
                con.print(
                    "[vex.muted]pipx upgrade failed - reinstalling from source[/]"
                )
                rc = _run([shutil.which("pipx") or "pipx", "install", "--force", src])
        elif method == "venv":
            con.print(
                f"[vex.muted]running: {sys.executable} -m pip install "
                f"--upgrade {src}[/]"
            )
            rc = _run([sys.executable, "-m", "pip", "install", "--upgrade", src])
        else:
            con.print(
                f"[vex.muted]running: {sys.executable} -m pip install "
                f"--upgrade {src}[/]"
            )
            rc = _run([sys.executable, "-m", "pip", "install", "--upgrade", src])
    except OSError as exc:
        err.print(f"[vex.error]error: could not launch the installer: {exc}[/]")
        return 3  # environment error: the local toolchain is broken

    if rc != 0:
        err.print(f"[vex.error]upgrade command failed (exit {rc})[/]")
        return 1
    new = installed_version()
    if new:
        con.print(f"[vex.ok]updated to {new}[/]")
    else:
        con.print("[vex.ok]upgrade finished[/]")
    con.print("[vex.muted]verify: vex --version[/]")
    return 0


def add_update_parser(sub) -> None:
    """Wire the `update` subcommand onto the CLI's subparsers."""
    p = sub.add_parser(
        "update",
        help="self-update Vex (or --check: report the latest release)",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="only report installed vs latest version (no upgrade)",
    )
    p.set_defaults(func=cmd_update)
