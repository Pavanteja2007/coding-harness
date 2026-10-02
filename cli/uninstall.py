"""Safe release uninstallation for Neo.

The uninstaller only removes paths whose resolved identity is inside a
documented Neo-owned root. Installer roots are fixed locations, while
configuration overrides select individual files and never grant ownership
of their parent directory.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from cli.neoconfig import global_settings_path, legacy_settings_path

PACKAGE_NAME = "neo-agent-cli"
# ``neo-harness`` was the previous working name of this distribution. It was
# never uploaded to PyPI, but it WAS installable from a local build
# (``pip install -e .`` before the rename), so a machine that did that still has
# that distribution on it. ``neo uninstall`` has to recognise it or it reports
# "nothing Neo-created found" while the distribution it owns is still installed.
# The guard below validates the distribution name against this tuple, so the
# entry is load-bearing, not documentation.
PACKAGE_NAMES = (PACKAGE_NAME, "neo-harness")
ENTRY_POINTS = ("neo", "harness")
_PIP_NOTE = "package-tool: pip "
_PIPX_NOTE = "package-tool: pipx "
_PATH_NOTE = "path-cleanup:"
_UNSAFE_NOTE = "unsafe:"
_PRESERVED_NOTE = "preserved:"
_CONFIG_FILES = ("settings.toml", "connectors.toml")
_LEGACY_CONFIG_FILES = ("config.toml", "install-route")
_CONFIG_DIRS = ("plugins", "commands", "skills")
_PROFILE_NAMES = (".bashrc", ".bash_profile", ".profile", ".zshrc")
_PROFILE_MARKER = "NEO_INSTALLER_PATH"
_LEGACY_PROFILE_MARKER = "# Added by the Neo installer"
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_WINDOWS_PIP_HELPER = r"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys

args = sys.argv[1:]
if len(args) != 6:
    raise SystemExit(2)
launcher_pid = int(args[0])
launcher_path = Path(args[1]).resolve()
target_prefix = Path(args[2]).resolve()
target_python = Path(args[3]).resolve()
distribution = args[4]
receipt = Path(args[5]).resolve()
if distribution not in {"neo-agent-cli", "neo-harness"}:
    raise SystemExit(3)
scripts = target_prefix / "Scripts"
if launcher_path.parent != scripts or launcher_path.name.casefold() not in {
    "neo.exe", "harness.exe"
}:
    raise SystemExit(4)
if not target_python.is_relative_to(target_prefix) or target_python.name.casefold() not in {
    "python.exe", "pythonw.exe"
}:
    raise SystemExit(5)
if not receipt.name.startswith("neo-uninstall-") or receipt.suffix != ".json":
    raise SystemExit(6)
if not receipt.parent.is_dir() or receipt.parent.is_symlink():
    raise SystemExit(7)

def write_receipt(status, **details):
    payload = {
        "schema_version": 1,
        "status": status,
        "helper_interpreter": str(Path(sys.executable).resolve()),
        "launcher_path": str(launcher_path),
        "launcher_pid": launcher_pid,
        "target_interpreter": str(target_python),
        "target_prefix": str(target_prefix),
        "distribution": distribution,
        **details,
    }
    temporary = receipt.with_name(receipt.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, receipt)

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel.GetProcessId.argtypes = [wintypes.HANDLE]
kernel.GetProcessId.restype = wintypes.DWORD
kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel.WaitForSingleObject.restype = wintypes.DWORD
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL
handle = kernel.OpenProcess(0x00100000 | 0x00001000, False, launcher_pid)
if not handle:
    raise SystemExit(8)
try:
    if kernel.GetProcessId(handle) != launcher_pid:
        raise SystemExit(9)
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        raise SystemExit(10)
    if Path(buffer.value).resolve() != launcher_path:
        raise SystemExit(11)
    write_receipt("waiting")
    if kernel.WaitForSingleObject(handle, 0xFFFFFFFF) != 0:
        raise RuntimeError("waiting for the console launcher failed")
finally:
    kernel.CloseHandle(handle)

allowed_environment = {
    "APPDATA", "COMSPEC", "LOCALAPPDATA", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE",
    "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR",
}
environment = {
    key: value for key, value in os.environ.items() if key.upper() in allowed_environment
}
environment.update({
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    "PYTHONNOUSERSITE": "1",
})
try:
    completed = subprocess.run(
        [str(target_python), "-I", "-m", "pip", "uninstall", "-y", distribution],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        shell=False,
        env=environment,
    )
    output = "\n".join(
        value for value in (completed.stdout.strip(), completed.stderr.strip()) if value
    )[-4000:]
    if completed.returncode != 0:
        raise RuntimeError(output or f"pip exited {completed.returncode}")
    removed = []
    remaining = []
    for name in ("neo.exe", "harness.exe"):
        path = scripts / name
        if path.exists():
            remaining.append(str(path))
        else:
            removed.append(str(path))
    if remaining:
        raise RuntimeError("entry points remain: " + ", ".join(remaining))
    write_receipt("succeeded", entry_points_removed=removed, output=output)
except Exception as exc:
    try:
        write_receipt("failed", error=f"{type(exc).__name__}: {exc}"[-4000:])
    except Exception:
        pass
    raise SystemExit(1)
"""


def _windows() -> bool:
    """Return whether the host uses Windows path and registry semantics."""
    return os.name == "nt"


def _resolve(path: Path) -> Optional[Path]:
    """Resolve a path without raising, including for broken symlinks."""
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _normal_path(path: Path) -> str:
    """Return a normalized, case-folded path key for containment checks."""
    resolved = _resolve(path)
    value = str(resolved if resolved is not None else path)
    value = os.path.normcase(os.path.normpath(value))
    return value.casefold() if _windows() else value


def _same_path(left: Path, right: Path) -> bool:
    """Return whether two paths resolve to the same filesystem identity."""
    left_resolved = _resolve(left)
    right_resolved = _resolve(right)
    if left_resolved is None or right_resolved is None:
        return False
    if left_resolved == right_resolved or _normal_path(left_resolved) == _normal_path(
        right_resolved
    ):
        return True
    try:
        return (
            left_resolved.exists()
            and right_resolved.exists()
            and os.path.samefile(left_resolved, right_resolved)
        )
    except (OSError, ValueError):
        return False


def _is_within(path: Path, root: Path) -> bool:
    """Return whether path is root or a descendant of root after resolution."""
    path_resolved = _resolve(path)
    root_resolved = _resolve(root)
    if path_resolved is None or root_resolved is None:
        return False
    try:
        path_resolved.relative_to(root_resolved)
        return True
    except ValueError:
        if not _windows():
            return False
        path_key = _normal_path(path_resolved)
        root_key = _normal_path(root_resolved)
        return path_key == root_key or path_key.startswith(root_key + os.sep)


def _path_exists(path: Path) -> bool:
    """Return whether a path exists, including a dangling symbolic link."""
    try:
        return path.exists() or path.is_symlink()
    except (OSError, ValueError):
        return False


def _safe_root(root: Path) -> bool:
    """Return whether root itself is not a symlink and can be resolved."""
    try:
        if root.is_symlink():
            return False
    except (OSError, ValueError):
        return False
    return _resolve(root) is not None


def _unique_paths(paths: Sequence[Path]) -> List[Path]:
    """Return paths in input order with resolved duplicates removed."""
    result: List[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = _normal_path(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _default_global_root() -> Path:
    """Return the platform global Neo configuration root without overrides."""
    if _windows():
        base = os.environ.get("APPDATA")
        root = Path(base).expanduser() if base else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "neo"


def _legacy_root() -> Path:
    """Return the documented legacy Neo root used for config and shims."""
    return Path.home() / ".neo"


def documented_config_roots() -> List[Path]:
    """Return fixed Neo-owned configuration roots, never override parents."""
    return _unique_paths(
        [
            _default_global_root(),
            Path.home() / ".config" / "neo",
            _legacy_root(),
        ]
    )


def settings_roots() -> List[Path]:
    """Return documented Neo config roots without trusting custom parents."""
    return documented_config_roots()


def _effective_config_roots() -> List[Path]:
    """Return safe roots for cleanup, excluding injected custom parents."""
    roots = settings_roots()
    if os.environ.get("NEO_CONFIG") or os.environ.get("NEO_LEGACY_CONFIG"):
        fixed_roots = documented_config_roots()
        roots = [
            root
            for root in roots
            if any(_same_path(root, fixed) for fixed in fixed_roots)
        ]
    return [root for root in roots if _safe_root(root)]


def installer_venv() -> Path:
    """Return the fixed installer virtual-environment location."""
    return Path.home() / ".neo-venv"


def installer_bin() -> Path:
    """Return the fixed installer shim directory location."""
    return Path.home() / ".neo" / "bin"


def _pipx_candidates() -> List[Path]:
    """Return known pipx venv candidates without probing or deleting them."""
    candidates: List[Path] = []
    pipx_home = os.environ.get("PIPX_HOME")
    if pipx_home:
        candidates.append(Path(pipx_home).expanduser() / "venvs" / PACKAGE_NAME)
    local_venvs = os.environ.get("PIPX_LOCAL_VENVS")
    if local_venvs:
        candidates.append(Path(local_venvs).expanduser() / PACKAGE_NAME)
    candidates.extend(
        [
            Path.home() / ".local" / "pipx" / "venvs" / PACKAGE_NAME,
            Path.home() / ".local" / "share" / "pipx" / "venvs" / PACKAGE_NAME,
        ]
    )
    return _unique_paths(candidates)


def _pipx_venv_from_executable() -> Optional[Path]:
    """Return the current interpreter's pipx venv when its layout is standard."""
    try:
        executable = Path(sys.executable).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for parent in executable.parents:
        if parent.name.casefold() != PACKAGE_NAME.casefold():
            continue
        if parent.parent.name.casefold() == "venvs":
            return parent
    return None


def pipx_venv_dir() -> Optional[Path]:
    """Return a known pipx venv path, or None when no candidate is present."""
    current = _pipx_venv_from_executable()
    candidates = ([current] if current is not None else []) + _pipx_candidates()
    for candidate in candidates:
        if _path_exists(candidate) and (candidate.is_dir() or candidate.is_symlink()):
            return candidate
    return None


def _pipx_bin_candidates() -> List[Path]:
    """Return documented pipx public-bin locations for PATH cleanup."""
    candidates: List[Path] = []
    configured = os.environ.get("PIPX_BIN_DIR")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([Path.home() / ".local" / "bin", Path.home() / "bin"])
    return _unique_paths(candidates)


def _path_cleanup_targets() -> List[Path]:
    """Return installer-owned public command directories that may be on PATH."""
    return _unique_paths([installer_bin(), *_pipx_bin_candidates()])


def _profile_files() -> List[Path]:
    """Return shell profile files that the POSIX installer may modify."""
    home = Path.home()
    profiles = [home / name for name in _PROFILE_NAMES]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg).expanduser() if xdg else home / ".config"
    profiles.append(config_home / "fish" / "config.fish")
    return _unique_paths(profiles)


def _path_text(path: Path) -> str:
    """Return a path string suitable for PATH and profile comparisons."""
    resolved = _resolve(path)
    return str(resolved if resolved is not None else path)


def _same_path_text(value: str, target: Path) -> bool:
    """Return whether a PATH/profile entry denotes exactly target."""
    candidate = value.strip().strip('"')
    if not candidate:
        return False
    try:
        candidate = os.path.expandvars(candidate)
        return _same_path(Path(candidate), target)
    except (OSError, ValueError):
        return False


def _profile_path_line_targets(line: str, target: Path) -> bool:
    """Return whether a profile line contains the installer PATH target."""
    if "PATH" not in line.upper():
        return False
    target_text = _path_text(target)
    variants = {target_text, target_text.replace("\\", "/")}
    return (
        any(variant and variant in line for variant in variants)
        or ".neo/bin" in line
        or "PIPX_BIN_DIR" in line
    )


def _profile_line_targets(line: str, target: Path) -> bool:
    """Return whether a profile line is marked as installer-owned."""
    marked = _PROFILE_MARKER in line or _LEGACY_PROFILE_MARKER in line
    return marked and _profile_path_line_targets(line, target)


def _profile_contains_installer_path(text: str, target: Path) -> bool:
    """Return whether a marked profile block contains the target PATH line."""
    marker_pending = False
    for line in text.splitlines():
        if _profile_line_targets(line, target):
            return True
        if _PROFILE_MARKER in line or _LEGACY_PROFILE_MARKER in line:
            marker_pending = True
            continue
        if marker_pending and _profile_path_line_targets(line, target):
            return True
        marker_pending = False
    return False


def _bin_on_user_path(bin_dir: Path) -> bool:
    """Return whether the exact installer shim directory is on user PATH."""
    if _windows():
        values = os.environ.get("PATH", "").split(os.pathsep)
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
            ) as key:
                registry_value, _value_type = winreg.QueryValueEx(key, "Path")
                values.extend(str(registry_value).split(";"))
        except Exception:
            pass
        return any(_same_path_text(value, bin_dir) for value in values)

    values = os.environ.get("PATH", "").split(os.pathsep)
    if any(_same_path_text(value, bin_dir) for value in values):
        return True
    for profile in _profile_files():
        try:
            text = profile.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if _profile_contains_installer_path(text, bin_dir):
            return True
    return False


def _remove_profile_path_entry(bin_dir: Path) -> Tuple[int, str]:
    """Remove only complete installer-marked PATH blocks from profiles."""
    changed = False
    try:
        for profile in _profile_files():
            if not profile.is_file() or profile.is_symlink():
                continue
            text = profile.read_text(encoding="utf-8")
            lines = text.splitlines(keepends=True)
            kept: List[str] = []
            profile_changed = False
            marker_pending = False
            for line in lines:
                if _PROFILE_MARKER in line or _LEGACY_PROFILE_MARKER in line:
                    profile_changed = True
                    marker_pending = True
                    continue
                if marker_pending and _profile_path_line_targets(line, bin_dir):
                    marker_pending = False
                    profile_changed = True
                    continue
                marker_pending = False
                if _profile_line_targets(line, bin_dir):
                    profile_changed = True
                    continue
                kept.append(line)
            if profile_changed:
                profile.write_text("".join(kept), encoding="utf-8")
                changed = True
    except (OSError, UnicodeError) as exc:
        return 1, f"could not edit installer PATH profiles: {exc}"
    if changed:
        return 0, f"removed {bin_dir} from installer PATH profiles"
    return 0, "PATH entry already absent"


def _remove_user_path_entry(bin_dir: Path) -> Tuple[int, str]:
    """Remove the exact installer shim path from persistent user PATH."""
    if not _windows():
        return _remove_profile_path_entry(bin_dir)
    try:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS
            ) as key:
                try:
                    value, _value_type = winreg.QueryValueEx(key, "Path")
                except FileNotFoundError:
                    return 0, "PATH entry already absent"
                parts = str(value).split(";")
                new_parts = [
                    part for part in parts if not _same_path_text(part, bin_dir)
                ]
                if len(new_parts) == len(parts):
                    return 0, "PATH entry already absent"
                winreg.SetValueEx(
                    key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(new_parts)
                )
        except FileNotFoundError:
            return 0, "PATH entry already absent"
        return 0, f"removed {bin_dir} from the user PATH"
    except Exception as exc:
        return 1, f"could not edit the user PATH: {exc}"


def _source_checkout_root() -> Optional[Path]:
    """Return the repository root when running from a source checkout."""
    try:
        root = Path(__file__).resolve().parent.parent
        if (root / ".git").exists() and (root / "pyproject.toml").is_file():
            return root
    except (OSError, ValueError):
        pass
    return None


def _running_from_source_checkout() -> bool:
    """Return whether this module is being loaded from the source checkout."""
    return _source_checkout_root() is not None


def _distribution_metadata_path(distribution: Any) -> Optional[Path]:
    """Return a distribution metadata path when the backend exposes one."""
    raw_path = getattr(distribution, "_path", None)
    if raw_path is None:
        return None
    return _resolve(Path(raw_path))


def _canonical_distribution_name(name: str) -> Optional[str]:
    """Return the supported Neo distribution name for metadata text."""
    normalized = name.strip().lower().replace("_", "-")
    for supported in PACKAGE_NAMES:
        if normalized == supported.lower().replace("_", "-"):
            return supported
    return None


def pip_distribution_name() -> Optional[str]:
    """Return the Neo distribution name installed for this interpreter."""
    try:
        from importlib.metadata import PackageNotFoundError, distribution, distributions

        source_root = _source_checkout_root()
        source_only = False
        for name in PACKAGE_NAMES:
            try:
                found = distribution(name)
            except PackageNotFoundError:
                continue
            metadata_path = _distribution_metadata_path(found)
            if (
                source_root is not None
                and metadata_path is not None
                and _is_within(metadata_path, source_root)
            ):
                source_only = True
                continue
            return name
        if source_only:
            for found in distributions():
                try:
                    metadata_name = found.metadata.get("Name", "")
                    name = _canonical_distribution_name(str(metadata_name))
                    if name is None:
                        continue
                    metadata_path = _distribution_metadata_path(found)
                    if metadata_path is None or not _is_within(
                        metadata_path, source_root
                    ):
                        return name
                except Exception:
                    continue
    except Exception:
        return None
    return None


def pip_distribution_installed() -> bool:
    """Return whether the current interpreter has a Neo pip distribution."""
    return pip_distribution_name() is not None


def pip_installed() -> bool:
    """Return whether plain pip should remove the Neo distribution."""
    return pip_distribution_installed()


def pip_uninstall_command(distribution_name: Optional[str] = None) -> List[str]:
    """Return the non-interactive plain-pip distribution uninstall command."""
    target = distribution_name or pip_distribution_name() or PACKAGE_NAME
    return [sys.executable, "-m", "pip", "uninstall", "-y", target]


def pip_entry_point_paths(distribution_name: Optional[str] = None) -> List[Path]:
    """Return existing console-script files owned by the installed distribution."""
    try:
        from importlib.metadata import PackageNotFoundError, distribution

        target = distribution_name or pip_distribution_name() or PACKAGE_NAME
        try:
            dist = distribution(target)
        except PackageNotFoundError:
            return []
        paths: List[Path] = []
        for file in dist.files or []:
            filename = Path(str(file)).name
            if filename.endswith(".exe"):
                filename = filename[:-4]
            if filename not in ENTRY_POINTS:
                continue
            path = Path(str(dist.locate_file(file)))
            if _path_exists(path):
                paths.append(path)
        return _unique_paths(paths)
    except Exception:
        return []


def _pipx_executable() -> Optional[str]:
    """Return the resolved pipx executable, or None when it is unavailable."""
    return shutil.which("pipx")


def pipx_uninstall_command(executable: Optional[str] = None) -> List[str]:
    """Return a safe fixed-argv pipx uninstall command for neo-agent-cli."""
    command = executable or _pipx_executable()
    if not command:
        raise RuntimeError("pipx executable was not found")
    return [str(command), "uninstall", PACKAGE_NAME]


def _run_tool(command: Sequence[str]) -> Tuple[int, str]:
    """Run a fixed-argv cleanup tool and return its code and output."""
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=300,
        )
    except Exception as exc:
        return 1, str(exc)
    output_parts = []
    for stream in (getattr(result, "stdout", ""), getattr(result, "stderr", "")):
        if stream:
            output_parts.append(str(stream).rstrip())
    try:
        return_code = int(getattr(result, "returncode", 1))
    except (TypeError, ValueError):
        return_code = 1
    return return_code, "\n".join(output_parts)


def _run_pip_uninstall(
    distribution_name: Optional[str] = None,
) -> Tuple[int, str]:
    """Uninstall the plain-pip distribution and both console entry points."""
    return _run_tool(pip_uninstall_command(distribution_name))


def _run_pipx_uninstall() -> Tuple[int, str]:
    """Uninstall the pipx-managed distribution through pipx itself."""
    try:
        command = pipx_uninstall_command()
    except RuntimeError as exc:
        return 1, str(exc)
    return _run_tool(command)


def _append_target(targets: List[Path], target: Path) -> None:
    """Append a removal target unless its resolved identity is already queued."""
    key = _normal_path(target)
    if all(_normal_path(existing) != key for existing in targets):
        targets.append(target)


def _config_cleanup_targets() -> Tuple[List[Path], List[str], List[str]]:
    """Return safe config targets plus preservation and refusal notes."""
    targets: List[Path] = []
    notes: List[str] = []
    failures: List[str] = []
    try:
        all_roots = settings_roots()
        roots = _effective_config_roots()
    except Exception as exc:
        return [], [f"{_UNSAFE_NOTE}could not inspect config roots: {exc}"], []

    for root in all_roots:
        if not _safe_root(root):
            failures.append(f"{_UNSAFE_NOTE}refused unsafe config root: {root}")
    safe_roots = roots

    configured = []
    for label, getter, env_name in (
        ("global", global_settings_path, "NEO_CONFIG"),
        ("legacy", legacy_settings_path, "NEO_LEGACY_CONFIG"),
    ):
        try:
            configured.append((label, Path(getter()), env_name))
        except Exception as exc:
            failures.append(f"{_UNSAFE_NOTE}could not inspect {label} config: {exc}")

    for label, path, env_name in configured:
        if not _path_exists(path):
            continue
        if not any(_is_within(path, root) for root in safe_roots):
            if os.environ.get(env_name):
                notes.append(
                    f"{_PRESERVED_NOTE}{label} config outside documented Neo roots: {path}"
                )
            continue
        if path.is_symlink():
            resolved = _resolve(path)
            if resolved is None or not any(
                _is_within(resolved, root) for root in safe_roots
            ):
                failures.append(
                    f"{_UNSAFE_NOTE}refused config symlink resolving outside Neo roots: {path}"
                )
            else:
                _append_target(targets, path)
        elif path.is_file():
            _append_target(targets, path)
        else:
            failures.append(f"{_UNSAFE_NOTE}refused non-file config path: {path}")

    for root in safe_roots:
        file_names = _CONFIG_FILES
        if _same_path(root, _legacy_root()):
            file_names = _LEGACY_CONFIG_FILES
        for name in file_names:
            child = root / name
            if not _path_exists(child):
                continue
            if child.is_symlink():
                resolved = _resolve(child)
                if resolved is None or not _is_within(resolved, root):
                    failures.append(
                        f"{_UNSAFE_NOTE}refused config symlink resolving outside Neo roots: {child}"
                    )
                else:
                    _append_target(targets, child)
            elif child.is_file():
                _append_target(targets, child)
            else:
                failures.append(f"{_UNSAFE_NOTE}refused non-file config path: {child}")
        for name in _CONFIG_DIRS:
            child = root / name
            if not _path_exists(child):
                continue
            if child.is_symlink():
                resolved = _resolve(child)
                if resolved is None or not _is_within(resolved, root):
                    failures.append(
                        f"{_UNSAFE_NOTE}refused config directory symlink outside Neo roots: {child}"
                    )
                else:
                    _append_target(targets, child)
            elif child.is_dir():
                _append_target(targets, child)
            else:
                notes.append(f"{_PRESERVED_NOTE}kept non-directory Neo path: {child}")
    return targets, notes, failures


def _config_root_items(targets: Sequence[Path], roots: Sequence[Path]) -> List[str]:
    """Return human-readable descriptions for roots containing owned targets."""
    items: List[str] = []
    seen: set[str] = set()
    for root in roots:
        if not any(_is_within(target, root) for target in targets):
            continue
        key = _normal_path(root)
        if key in seen:
            continue
        seen.add(key)
        items.append(f"config directory: {root} (owned files and known children only)")
    return items


def _executable_in_tree(executable: Path, root: Path) -> bool:
    """Return whether a running executable is the root or below it."""
    return _is_within(executable, root)


def _process_uses_venv(root: Path) -> bool:
    """Return whether the current process has a path inside root."""
    candidates = [Path(sys.executable)]
    try:
        if sys.argv and sys.argv[0]:
            candidates.append(Path(sys.argv[0]))
    except (OSError, ValueError):
        pass
    try:
        candidates.append(Path(__file__))
    except (OSError, ValueError):
        pass
    return any(_executable_in_tree(candidate, root) for candidate in candidates)


def _outside_python(venv: Path) -> Optional[Path]:
    """Find an interpreter outside venv for a deferred Windows cleanup."""
    candidates: List[Path] = []
    base_executable = getattr(sys, "_base_executable", "")
    if base_executable:
        candidates.append(Path(base_executable))
    base_prefix = Path(getattr(sys, "base_prefix", sys.prefix))
    candidates.extend([base_prefix / "python.exe", base_prefix / "python"])
    for candidate in _unique_paths(candidates):
        if not _path_exists(candidate) or not candidate.is_file():
            continue
        resolved = _resolve(candidate)
        if resolved is None or _is_within(resolved, venv):
            continue
        try:
            if _same_path(resolved, Path(sys.executable)):
                continue
        except OSError:
            pass
        return resolved
    return None


def _windows_process_table() -> dict[int, Tuple[int, str]]:
    """Return current Windows process IDs, parent IDs, and image names."""
    if not _windows():
        return {}
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry),
    ]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return {}
    result: dict[int, Tuple[int, str]] = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            return result
        while True:
            result[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID),
                str(entry.szExeFile),
            )
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel.CloseHandle(snapshot)
    return result


def _windows_process_image(pid: int) -> Optional[Path]:
    """Return one Windows process image path after a limited-info query."""
    if not _windows():
        return None
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00001000, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value).resolve()
    finally:
        kernel.CloseHandle(handle)


def _windows_launcher_process(
    entry_points: Sequence[Path],
) -> Optional[Tuple[int, Path]]:
    """Return the outermost active Neo console launcher in this process ancestry."""
    owned = {
        _normal_path(path): path.resolve()
        for path in _unique_paths(list(entry_points))
        if _path_exists(path)
    }
    if not owned:
        return None
    table = _windows_process_table()
    pid = os.getpid()
    seen: set[int] = set()
    matches: list[Tuple[int, Path]] = []
    while pid and pid not in seen:
        seen.add(pid)
        image = _windows_process_image(pid)
        if image is not None:
            match = owned.get(_normal_path(image))
            if match is not None:
                matches.append((pid, match))
        row = table.get(pid)
        if row is None:
            break
        pid = row[0]
    return matches[-1] if matches else None


def _helper_environment() -> dict[str, str]:
    """Return a minimal credential-free environment for deferred uninstall helpers."""
    allowed = {
        "APPDATA",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return environment


def _new_uninstall_receipt() -> Path:
    """Create an empty private receipt path for a deferred uninstall helper."""
    descriptor, raw_path = tempfile.mkstemp(
        prefix="neo-uninstall-",
        suffix=".json",
    )
    os.close(descriptor)
    return Path(raw_path).resolve()


def _wait_for_uninstall_receipt(
    receipt: Path,
    timeout_s: float = 15.0,
) -> Optional[dict[str, Any]]:
    """Wait until the helper proves it holds the validated launcher process handle."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
            continue
        if isinstance(value, dict) and value.get("status") in {"waiting", "failed"}:
            return value
        time.sleep(0.05)
    return None


def _schedule_windows_pip_uninstall(
    distribution_name: str,
    entry_points: Sequence[Path],
) -> Tuple[int, str]:
    """Schedule pip after the outer Windows console launcher releases its files."""
    if distribution_name not in PACKAGE_NAMES:
        return 1, "refused to uninstall an unsupported distribution"
    launcher = _windows_launcher_process(entry_points)
    if launcher is None:
        return 1, "could not identify the active Neo console launcher"
    launcher_pid, launcher_path = launcher
    target_prefix = Path(sys.prefix).resolve()
    target_python = Path(sys.executable).resolve()
    outside = _outside_python(target_prefix)
    if outside is None:
        return 1, "no external interpreter is available for deferred pip cleanup"
    if not target_python.is_relative_to(target_prefix):
        return 1, "the active interpreter is outside its reported virtual environment"
    receipt = _new_uninstall_receipt()
    try:
        process = subprocess.Popen(
            [
                str(outside),
                "-I",
                "-u",
                "-c",
                _WINDOWS_PIP_HELPER,
                str(launcher_pid),
                str(launcher_path),
                str(target_prefix),
                str(target_python),
                distribution_name,
                str(receipt),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=_helper_environment(),
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        )
    except Exception as exc:
        try:
            receipt.unlink()
        except OSError:
            pass
        return 1, f"could not schedule external pip cleanup: {exc}"
    waiting = _wait_for_uninstall_receipt(receipt)
    if waiting is None or waiting.get("status") != "waiting":
        try:
            process.kill()
        except Exception:
            pass
        error = waiting.get("error") if isinstance(waiting, dict) else None
        try:
            receipt.unlink()
        except OSError:
            pass
        return (
            1,
            f"external pip cleanup did not reach the launcher: {error or 'timeout'}",
        )
    return 0, (
        "scheduled pip uninstall through an external interpreter after the console "
        f"launcher exits; receipt={receipt}"
    )


def _schedule_windows_venv_cleanup(venv: Path) -> Tuple[int, str]:
    """Schedule venv removal with an interpreter outside that venv."""
    outside = _outside_python(venv)
    if outside is None:
        return 1, (
            f"refused to remove running Neo venv {venv}; no interpreter outside it "
            "was available"
        )
    cleanup_code = (
        "import ctypes,os,shutil,sys,time\n"
        "pid=int(sys.argv[1])\n"
        "target=sys.argv[2]\n"
        "parent=sys.argv[3]\n"
        "if os.path.basename(target) != '.neo-venv':\n"
        "    raise SystemExit(2)\n"
        "if os.path.islink(target):\n"
        "    raise SystemExit(3)\n"
        "if os.path.dirname(os.path.realpath(target)) != os.path.realpath(parent):\n"
        "    raise SystemExit(4)\n"
        "if os.name == 'nt':\n"
        "    handle=ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)\n"
        "    if not handle:\n"
        "        raise SystemExit(5)\n"
        "    try:\n"
        "        while True:\n"
        "            result=ctypes.windll.kernel32.WaitForSingleObject(handle, 200)\n"
        "            if result == 0x00000102:\n"
        "                time.sleep(0.2)\n"
        "                continue\n"
        "            if result != 0:\n"
        "                raise SystemExit(6)\n"
        "            break\n"
        "    finally:\n"
        "        ctypes.windll.kernel32.CloseHandle(handle)\n"
        "else:\n"
        "    while True:\n"
        "        try:\n"
        "            os.kill(pid, 0)\n"
        "        except PermissionError:\n"
        "            time.sleep(0.2)\n"
        "        except OSError:\n"
        "            break\n"
        "if os.path.isdir(target):\n"
        "    shutil.rmtree(target)\n"
    )
    try:
        subprocess.Popen(
            [
                str(outside),
                "-I",
                "-c",
                cleanup_code,
                str(os.getpid()),
                str(_resolve(venv) or venv),
                str(_resolve(Path.home()) or Path.home()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as exc:
        return 1, f"could not schedule Neo venv cleanup: {exc}"
    return 0, f"scheduled removal of running Neo venv {venv} after exit"


def _schedule_windows_pipx_uninstall(venv: Path) -> Tuple[int, str]:
    """Schedule pipx removal after a Windows pipx-hosted process exits."""
    outside = _outside_python(venv)
    executable = _pipx_executable()
    if outside is None or executable is None:
        return 1, "could not schedule pipx removal from the running pipx environment"
    cleanup_code = (
        "import ctypes,os,subprocess,sys,time\n"
        "pid=int(sys.argv[1])\n"
        "executable=sys.argv[2]\n"
        "target=sys.argv[3]\n"
        "if os.path.basename(target).lower() != 'neo-agent-cli':\n"
        "    raise SystemExit(2)\n"
        "if os.path.basename(os.path.dirname(target)).lower() != 'venvs':\n"
        "    raise SystemExit(3)\n"
        "handle=ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)\n"
        "if not handle:\n"
        "    raise SystemExit(4)\n"
        "try:\n"
        "    while True:\n"
        "        result=ctypes.windll.kernel32.WaitForSingleObject(handle, 200)\n"
        "        if result == 0x00000102:\n"
        "            time.sleep(0.2)\n"
        "            continue\n"
        "        if result != 0:\n"
        "            raise SystemExit(5)\n"
        "        break\n"
        "finally:\n"
        "    ctypes.windll.kernel32.CloseHandle(handle)\n"
        "result=subprocess.run([executable, 'uninstall', 'neo-agent-cli'], check=False)\n"
        "raise SystemExit(result.returncode)\n"
    )
    try:
        subprocess.Popen(
            [
                str(outside),
                "-I",
                "-c",
                cleanup_code,
                str(os.getpid()),
                str(executable),
                str(_resolve(venv) or venv),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as exc:
        return 1, f"could not schedule pipx removal: {exc}"
    return 0, f"scheduled pipx removal of running Neo environment {venv} after exit"


def _directory_is_empty(path: Path) -> bool:
    """Return whether a real directory exists and has no children."""
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        return not any(path.iterdir())
    except (OSError, ValueError):
        return False


def _remove_empty_root(path: Path, roots: Sequence[Path]) -> Tuple[int, str]:
    """Remove a documented config root only when it is already empty."""
    if not _path_exists(path):
        return 0, f"already absent: {path}"
    if not any(_same_path(path, root) for root in roots):
        return 1, f"refused unexpected config root: {path}"
    if path.is_symlink():
        return 1, f"refused symlinked config root: {path}"
    resolved = _resolve(path)
    if resolved is None or not any(_is_within(resolved, root) for root in roots):
        return 1, f"refused path outside Neo-owned roots: {path}"
    try:
        path.rmdir()
    except FileNotFoundError:
        return 0, f"already absent: {path}"
    except OSError as exc:
        try:
            has_children = any(path.iterdir())
        except OSError:
            return 1, f"could not remove config root {path}: {exc}"
        if has_children:
            return 0, f"kept non-empty config root: {path}"
        return 1, f"could not remove empty config root {path}: {exc}"
    return 0, f"removed {path}"


def _remove_file(path: Path, roots: Sequence[Path]) -> Tuple[int, str]:
    """Remove one owned file or safe symlink after resolved containment."""
    if not _path_exists(path):
        return 0, f"already absent: {path}"
    resolved = _resolve(path)
    if resolved is None or not any(_is_within(resolved, root) for root in roots):
        return 1, f"refused path outside Neo-owned roots: {path}"
    if path.is_symlink():
        try:
            path.unlink()
        except FileNotFoundError:
            return 0, f"already absent: {path}"
        except OSError as exc:
            return 1, f"could not remove {path}: {exc}"
        return 0, f"removed {path}"
    if not path.is_file():
        return 1, f"refused non-file Neo path: {path}"
    try:
        path.unlink()
    except FileNotFoundError:
        return 0, f"already absent: {path}"
    except OSError as exc:
        return 1, f"could not remove {path}: {exc}"
    return 0, f"removed {path}"


def _remove_tree(
    path: Path, roots: Sequence[Path], exact: Optional[Path] = None
) -> Tuple[int, str]:
    """Remove one owned directory after resolved identity and containment."""
    if not _path_exists(path):
        return 0, f"already absent: {path}"
    if exact is not None and not _same_path(path, exact):
        return 1, f"refused unexpected Neo path: {path}"
    resolved = _resolve(path)
    if resolved is None or not any(_is_within(resolved, root) for root in roots):
        return 1, f"refused path outside Neo-owned roots: {path}"
    if path.is_symlink():
        return _remove_file(path, roots)
    if not path.is_dir():
        return 1, f"refused non-directory Neo path: {path}"
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return 0, f"already absent: {path}"
    except OSError as exc:
        return 1, f"could not remove {path}: {exc}"
    return 0, f"removed {path}"


def _remove_installer_venv(path: Path) -> Tuple[int, str]:
    """Remove the fixed installer venv, deferring safely on Windows."""
    expected = installer_venv()
    if not _same_path(path, expected):
        return 1, f"refused unexpected installer venv: {path}"
    if not _path_exists(path):
        return 0, f"already absent: {path}"
    if _windows() and _process_uses_venv(path):
        return _schedule_windows_venv_cleanup(path)
    return _remove_tree(path, [Path.home()], exact=expected)


def _remove_target(path: Path) -> Tuple[int, str]:
    """Remove a queued target using the narrowest safe operation."""
    try:
        if _same_path(path, installer_venv()):
            return _remove_installer_venv(path)
        if _same_path(path, installer_bin()):
            if not _safe_root(_legacy_root()):
                return (
                    1,
                    f"refused symlinked or unsafe installer root: {_legacy_root()}",
                )
            return _remove_tree(path, [_legacy_root()], exact=installer_bin())
    except Exception as exc:
        return 1, f"could not identify Neo-owned path {path}: {exc}"
    try:
        roots = _effective_config_roots()
    except Exception as exc:
        return 1, f"could not inspect Neo config roots: {exc}"
    if not any(_is_within(path, root) for root in roots):
        return 1, f"refused path outside Neo-owned roots: {path}"
    try:
        if any(_same_path(path, root) for root in roots):
            return _remove_empty_root(path, roots)
        if path.is_dir() and not path.is_symlink():
            return _remove_tree(path, roots)
        return _remove_file(path, roots)
    except OSError as exc:
        return 1, f"could not remove {path}: {exc}"


def _note_starts(note: str, prefix: str) -> bool:
    """Return whether an internal plan note has the requested prefix."""
    return note.startswith(prefix)


def collect_plan() -> Tuple[List[str], List[Path], List[str]]:
    """Return descriptions, safe removal targets, and operational notes."""
    items: List[str] = []
    targets: List[Path] = []
    notes: List[str] = []

    pipx_path = pipx_venv_dir()
    if pipx_path is not None:
        items.append(
            f"pipx distribution: {PACKAGE_NAME} at {pipx_path} "
            f"(pipx uninstall {PACKAGE_NAME})"
        )
        notes.append(f"{_PIPX_NOTE} pipx manages {PACKAGE_NAME}")

    venv = installer_venv()
    pip_here = pip_installed()
    running_installer_venv = _process_uses_venv(venv) if _path_exists(venv) else False
    if (
        pip_here
        and not running_installer_venv
        and not (
            pipx_path is not None
            and _executable_in_tree(Path(sys.executable), pipx_path)
        )
    ):
        items.append(
            f"pip distribution: {PACKAGE_NAME} via {' '.join(pip_uninstall_command())} "
            f"(pip uninstall {PACKAGE_NAME})"
        )
        notes.append(f"{_PIP_NOTE} plain pip distribution")

    if _path_exists(venv):
        items.append(f"installer venv: {venv}")
        _append_target(targets, venv)
    bin_dir = installer_bin()
    if _path_exists(bin_dir):
        items.append(f"installer PATH shims: {bin_dir}")
        _append_target(targets, bin_dir)

    config_targets, config_notes, config_failures = _config_cleanup_targets()
    try:
        config_roots = _effective_config_roots()
    except Exception:
        config_roots = []
    items.extend(_config_root_items(config_targets, config_roots))
    for target in config_targets:
        if not any(_is_within(target, root) for root in config_roots):
            items.append(f"config file: {target}")
        _append_target(targets, target)
    for root in _unique_paths(config_roots):
        if not _path_exists(root) or not root.is_dir() or root.is_symlink():
            continue
        owns_config = any(_is_within(target, root) for target in config_targets)
        owns_legacy_shim = (
            _same_path(root, _legacy_root())
            and _is_within(installer_bin(), root)
            and _path_exists(installer_bin())
        )
        if owns_config or owns_legacy_shim or _directory_is_empty(root):
            if not any(item.startswith(f"config directory: {root}") for item in items):
                items.append(
                    f"config directory: {root} (owned files and known children only)"
                )
            _append_target(targets, root)
    notes.extend(config_notes)
    notes.extend(config_failures)

    for path_target in _path_cleanup_targets():
        if _bin_on_user_path(path_target):
            notes.append(f"{_PATH_NOTE}user PATH entry: {path_target}")

    return list(dict.fromkeys(items)), _unique_paths(targets), notes


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove Neo-owned resources and report any incomplete cleanup."""
    from cli import ui

    con = ui.console()
    err = ui.err_console()
    try:
        items, targets, notes = collect_plan()
    except Exception as exc:
        err.print(f"[neo.error]could not build uninstall plan: {exc}[/]")
        return 1

    if not items and not notes:
        con.print("[neo.ok]nothing Neo-created found; Neo is already uninstalled[/]")
        con.print(
            "[neo.muted](a source checkout is not a Neo-owned install; remove "
            "the checkout separately)[/]"
        )
        return 0

    con.print("[neo.accent]this will remove:[/]")
    for item in items:
        con.print(f"  [neo.error]x[/] [neo.muted]{item}[/]")
    for note in notes:
        con.print(f"  [neo.warn]![/] [neo.muted]{note}[/]")

    if getattr(args, "dry_run", False):
        con.print("[neo.muted]dry run - nothing removed[/]")
        return 0

    if not getattr(args, "yes", False):
        try:
            answer = input("remove these? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            con.print("[neo.muted]aborted - nothing removed[/]")
            return 0

    failed = any(_note_starts(note, _UNSAFE_NOTE) for note in notes)
    pip_attempted = any(_note_starts(note, _PIP_NOTE) for note in notes)
    pip_cleanup_complete = False
    pending_cleanup = False

    if pip_attempted:
        distribution_name = pip_distribution_name() or PACKAGE_NAME
        entry_points_before = pip_entry_point_paths(distribution_name)
        scheduled = False
        if _windows() and _windows_launcher_process(entry_points_before) is not None:
            try:
                code, output = _schedule_windows_pip_uninstall(
                    distribution_name,
                    entry_points_before,
                )
            except Exception as exc:
                code, output = 1, str(exc)
            if code != 0:
                err.print(
                    f"[neo.error]could not schedule safe pip uninstall: {output}[/]"
                )
                return 1
            scheduled = True
            pending_cleanup = True
            con.print(f"[neo.muted]{output}[/]")
        else:
            try:
                code, output = _run_pip_uninstall(distribution_name)
            except Exception as exc:
                code, output = 1, str(exc)
        if not scheduled and code != 0:
            failed = True
            err.print(
                f"[neo.error]pip uninstall failed: {output or 'unknown error'}[/]"
            )
        elif not scheduled:
            remaining = [path for path in entry_points_before if _path_exists(path)]
            if remaining:
                failed = True
                err.print(
                    "[neo.error]pip returned success but entry points remain: "
                    f"{', '.join(str(path) for path in remaining)}[/]"
                )
            else:
                pip_cleanup_complete = True
                con.print(
                    "[neo.ok]removed[/] [neo.muted]neo-agent-cli distribution and "
                    f"{'/'.join(ENTRY_POINTS)} entry points[/]"
                )
                if output:
                    con.print(f"[neo.muted]{output}[/]")

    pipx_attempted = any(_note_starts(note, _PIPX_NOTE) for note in notes)
    if pipx_attempted:
        pipx_path_before = pipx_venv_dir()
        if (
            _windows()
            and pipx_path_before is not None
            and _process_uses_venv(pipx_path_before)
        ):
            try:
                code, output = _schedule_windows_pipx_uninstall(pipx_path_before)
            except Exception as exc:
                code, output = 1, str(exc)
        else:
            try:
                code, output = _run_pipx_uninstall()
            except Exception as exc:
                code, output = 1, str(exc)
        if code != 0:
            failed = True
            err.print(
                f"[neo.error]pipx uninstall failed: {output or 'pipx unavailable'}[/]"
            )
        elif output.startswith("scheduled pipx removal"):
            pending_cleanup = True
            con.print(f"[neo.muted]{output}[/]")
        elif pipx_path_before is not None and _path_exists(pipx_path_before):
            failed = True
            err.print(
                f"[neo.error]pipx returned success but its venv remains: {pipx_path_before}[/]"
            )
        else:
            con.print(
                "[neo.ok]removed[/] [neo.muted]pipx distribution neo-agent-cli[/]"
            )
            if output:
                con.print(f"[neo.muted]{output}[/]")

    path_targets = _unique_paths(
        [
            Path(note[len(_PATH_NOTE) :].removeprefix("user PATH entry: "))
            for note in notes
            if note.startswith(_PATH_NOTE)
        ]
    )
    for path_target in path_targets:
        try:
            code, message = _remove_user_path_entry(path_target)
        except Exception as exc:
            code, message = 1, str(exc)
        con.print(f"[neo.muted]PATH: {message}[/]")
        if code != 0:
            failed = True
            err.print("[neo.error]PATH cleanup did not complete[/]")

    for target in targets:
        if (
            pip_attempted
            and not pip_cleanup_complete
            and _same_path(target, installer_venv())
        ):
            failed = True
            err.print(
                "[neo.error]left installer venv because pip cleanup is pending or "
                f"failed: {target}[/]"
            )
            continue
        try:
            code, message = _remove_target(target)
        except Exception as exc:
            code, message = 1, str(exc)
        con.print(f"[neo.muted]{message}[/]")
        if code != 0:
            failed = True
            err.print(f"[neo.error]cleanup failed for {target}[/]")
        elif message.startswith("scheduled removal"):
            pending_cleanup = True

    if failed:
        err.print(
            "[neo.error]Neo uninstall incomplete; retry after fixing the error[/]"
        )
        return 1
    if pending_cleanup:
        con.print(
            "[neo.ok]Neo uninstall scheduled[/] [neo.muted](the external helper will "
            "finish after the active launcher exits)[/]"
        )
        return 0
    con.print(
        "[neo.ok]Neo uninstall complete[/] [neo.muted](open a new terminal "
        "for PATH changes)[/]"
    )
    return 0


def add_uninstall_parser(sub: Any) -> None:
    """Wire the ``neo uninstall`` subcommand into an argparse parser."""
    parser = sub.add_parser(
        "uninstall",
        help="remove Neo completely (config + venv + PATH entries)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be removed, remove nothing",
    )
    parser.set_defaults(func=cmd_uninstall)
