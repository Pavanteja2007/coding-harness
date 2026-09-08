"""STUB for INTERFACES.md Boundary 1 — Terminal 2 (execution) owns the real one.

Local subprocess stub: no Docker, no isolation, no network restrictions —
the repo is just a directory on this machine and the command runs directly.
Signature matches execution.sandbox.execute_sandboxed exactly so the import
in harness/deps.py can be swapped without any other code changing.

Command selection: on Windows we explicitly prefer Git-bash over
System32\\bash.exe (the WSL launcher) so python/pytest resolve to the
Windows toolchain the repo files belong to. On other platforms /bin/bash is
used. Set env HARNESS_SANDBOX_SHELL=0 to force cmd.exe instead of bash.
"""
import os
import subprocess
from typing import List, Optional

from shared.types import ExecutionResult

_BASH_CANDIDATES = [
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
    "/usr/bin/bash",
    "/bin/bash",
]

_bash_cache: Optional[str] = None


def _find_bash() -> Optional[str]:
    """Return a bash path or None. System32\\bash.exe (the WSL launcher) is
    deliberately skipped: it would run commands inside Linux/WSL against a
    Windows checkout, which is not what a local stub wants.
    """
    global _bash_cache
    if _bash_cache is not None:
        return _bash_cache
    found: Optional[str] = None
    if os.name == "nt":
        candidates = _BASH_CANDIDATES[:2]
        for path_dir in os.environ.get("PATH", "").split(os.pathsep):
            if not path_dir or "system32" in path_dir.lower():
                continue
            candidates.append(os.path.join(path_dir, "bash.exe"))
        for cand in candidates:
            if os.path.exists(cand):
                found = cand
                break
    else:
        for cand in _BASH_CANDIDATES[2:]:
            if os.path.exists(cand):
                found = cand
                break
    _bash_cache = found
    return found


def _to_str(val: object) -> str:
    """Best-effort decode of possibly-bytes subprocess output."""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val or ""


def _shell_argv(command: str) -> List[str]:
    """Build the argv for running `command` in the local shell."""
    use_bash = os.environ.get("HARNESS_SANDBOX_SHELL", "1") != "0"
    if use_bash:
        bash = _find_bash()
        if bash:
            return [bash, "-lc", command]
    return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]


def execute_sandboxed(repo_path: str, command: str, timeout_s: int = 120) -> ExecutionResult:
    """STUB: run `command` inside `repo_path` via a local shell, no isolation.

    Assumes repo_path is an existing directory on this machine and command
    is a single shell command line (may contain &&, pipes, etc.). Returns
    an ExecutionResult with exit_code 124 and timed_out=True on timeout.
    """
    try:
        proc = subprocess.run(
            _shell_argv(command),
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        return ExecutionResult(
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(
            exit_code=124,
            stdout=_to_str(exc.stdout),
            stderr=_to_str(exc.stderr),
            timed_out=True,
        )
