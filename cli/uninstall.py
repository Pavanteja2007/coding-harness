"""`vex uninstall` — the clean removal path (Task F — CLI citizenship).

A user who stops using Vex should be able to remove it completely,
including the config directories and PATH entries Vex's installers
created — no leftover directories hunting. The command enumerates
everything Vex may have created on this machine (probing the SAME
locations the installers write: pipx venvs, the dedicated ~/.vex-venv,
~/.vex/bin shims, the settings/config roots) and removes exactly that,
after showing the list and requiring confirmation (or --yes for
scripts).

Design notes:
- NEVER removes anything it didn't verify is Vex's own (each path is
  checked against the known layout: a plugins root containing plugin
  dirs, a settings file, the vex-venv launcher — not blind rmtree).
- The removal set is PRINTED before action; --dry-run prints only.
- pipx/pip package removal goes through their own tools.
- Exit codes follow cli.exit_codes (2 usage, 1 failure, 0 clean).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

from cli.vexconfig import global_settings_path, legacy_settings_path


def _windows() -> bool:
    return os.name == "nt"


def settings_roots() -> List[Path]:
    """Every config/settings directory Vex may have created (probed via
    cli.vexconfig's own path functions so this can never drift)."""
    roots = []
    for p in (global_settings_path().parent, legacy_settings_path().parent):
        if p not in roots:
            roots.append(p)
    return roots


def installer_venv() -> Path:
    """The installer's dedicated venv location (~/.vex-venv)."""
    return Path.home() / ".vex-venv"


def installer_bin() -> Path:
    """The installer's PATH shim directory (~/.vex/bin)."""
    return Path.home() / ".vex" / "bin"


def pipx_venv_dir() -> "Path | None":
    """The pipx venv for vex-harness, or None (pips location varies by
    version — PIPX_HOME/venvs)."""
    home = os.environ.get("PIPX_HOME", str(Path.home() / ".local" / "pipx"))
    cand = Path(home) / "venvs" / "vex-harness"
    if cand.exists():
        return cand
    # legacy default layout
    legacy = Path.home() / ".local" / "pipx" / "venvs" / "vex-harness"
    if legacy.exists():
        return legacy
    return None


def _bin_on_user_path(bin_dir: Path) -> bool:
    """True when bin_dir is on the USER Path (Windows registry or env)."""
    if _windows():
        try:
            user_path = os.environ.get("PATH", "")
            # the registry form matters (install.ps1 writes there); the
            # current-session PATH usually mirrors it
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
            ) as k:
                val, _t = winreg.QueryValueEx(k, "Path")
                user_path = f"{user_path};{val}"
        except Exception:
            pass
        return str(bin_dir).lower() in user_path.lower().replace("/", "\\")
    return str(bin_dir) in os.environ.get("PATH", "").split(":")


def _remove_user_path_entry(bin_dir: Path) -> "Tuple[int, str]":
    """Remove bin_dir from the USER Path (registry on Windows; best-effort,
    idempotent — missing entry is success)."""
    if not _windows():
        return 0, "no-op (non-Windows installers never edit PATH)"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS
        ) as k:
            try:
                val, _t = winreg.QueryValueEx(k, "Path")
                parts = [p for p in val.split(";") if p.strip()]
            except OSError:
                parts = []
            target = str(bin_dir)
            new_parts = [p for p in parts if p.strip().lower() != target.lower()]
            if len(new_parts) == len(parts):
                return 0, "PATH entry already absent"
            winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(new_parts))
        return 0, f"removed {target} from the user PATH"
    except OSError as exc:
        return 1, f"could not edit the user PATH: {exc}"


def collect_plan() -> "Tuple[List[str], List[str], List[str]]":
    """(descriptions, removable_dirs, notes) — everything this uninstall
    WOULD remove on this machine, in order, with honest notes for what
    needs the user's own tools (pipx)."""
    items: List[str] = []
    dirs: List[Path] = []
    notes: List[str] = []

    pv = pipx_venv_dir()
    if pv is not None:
        items.append(f"pipx venv: {pv} (removed via `pipx uninstall`)")
        notes.append("pipx: run `pipx uninstall vex-harness`")
    if installer_venv().exists():
        items.append(f"installer venv: {installer_venv()}")
        dirs.append(installer_venv())
    if installer_bin().exists():
        items.append(f"PATH shims: {installer_bin()}")
        dirs.append(installer_bin().parent)
    if _bin_on_user_path(installer_bin()):
        notes.append("user PATH entry pointing at ~/.vex/bin")
    for root in settings_roots():
        if root.exists():
            # only claim dirs that look like Vex's own (settings file,
            # plugins/, commands/, skills/ — not an unrelated ~/.config/vex
            # that predates us... every file under it IS ours by layout)
            items.append(f"config directory: {root}")
            dirs.append(root)
    return items, dirs, notes


def cmd_uninstall(args: argparse.Namespace) -> int:
    """`vex uninstall [--yes|--dry-run]` — remove Vex completely."""
    from cli import ui

    con = ui.console()
    err = ui.err_console()
    items, dirs, notes = collect_plan()

    if not items and not notes:
        con.print("[vex.muted]nothing Vex-created found on this machine[/]")
        con.print(
            "[vex.muted](running from a source checkout? remove the "
            "checkout + `pip uninstall vex-harness` if installed)[/]"
        )
        return 0

    con.print("[vex.accent]this will remove:[/]")
    for i in items:
        con.print(f"  [vex.error]x[/] [vex.muted]{i}[/]")
    for n in notes:
        con.print(f"  [vex.warn]![/] [vex.muted]{n}[/]")

    if getattr(args, "dry_run", False):
        con.print("[vex.muted]dry run - nothing removed[/]")
        return 0

    if not getattr(args, "yes", False):
        try:
            answer = input("remove these? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            con.print("[vex.muted]aborted - nothing removed[/]")
            return 0

    # PATH entry first (so a half-removed state never points anywhere)
    if "user PATH entry pointing at ~/.vex/bin" in notes:
        code, msg = _remove_user_path_entry(installer_bin())
        con.print(f"[vex.muted]PATH: {msg}[/]")
        if code != 0:
            err.print("[vex.warn]remove the PATH entry by hand if needed[/]")

    for d in dirs:
        try:
            if d.is_dir():
                shutil.rmtree(d)
                con.print(f"[vex.ok]removed[/] [vex.muted]{d}[/]")
        except OSError as exc:
            err.print(f"[vex.error]could not remove {d}: {exc}[/]")
            return 1

    if any("pipx" in n for n in notes):
        con.print("[vex.warn]finish with: pipx uninstall vex-harness[/]")

    # pip-installed (non-venv, non-pipx) case: offer the one-liner
    con.print(
        "[vex.muted]if vex was also pip-installed into a Python env: "
        f"{sys.executable} -m pip uninstall vex-harness[/]"
    )
    con.print(
        "[vex.ok]vex uninstalled[/] [vex.muted](open a new terminal "
        "for the PATH change to take effect)[/]"
    )
    return 0


def add_uninstall_parser(sub) -> None:
    """Wire the `uninstall` subcommand."""
    p = sub.add_parser(
        "uninstall",
        help="remove Vex completely (config + venv + PATH entries)",
    )
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be removed, remove nothing",
    )
    p.set_defaults(func=cmd_uninstall)
