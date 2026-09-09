"""Edit validation: snapshot, restore, diff extraction, and pre-test checks
(spec items 3 and the "patch validates cleanly" requirement).

The agent edits files directly in its working copy via bash (the chosen
action space), so "patch generation" here means: snapshot the pristine
copy, detect WHAT changed (difflib unified diff against the snapshot),
and validate changes BEFORE handing the state to the verifier:
- Python syntax check (compileall-style) on changed .py files
- protected-path check (config["protected_paths"] globs)
- optional size sanity (refuse absurdly large rewrites)

If validation fails, the loop controller can restore the working copy to
the last good state instead of wasting a verify run on garbage.
"""
import difflib
import fnmatch
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def snapshot(src: str, dst: str) -> None:
    """Copy a repo tree to dst (the agent's pristine reference copy).

    Assumes src is a directory; skips caches/logs so the diff stays clean.
    """
    src_p, dst_p = Path(src), Path(dst)
    skip = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".egg-info"}
    shutil.copytree(
        src_p, dst_p,
        ignore=shutil.ignore_patterns(*skip, "*.pyc", ".git"),
    )


def changed_files(pristine_dir: str, work_dir: str) -> List[str]:
    """Repo-relative posix paths that differ between pristine and working
    copies (added, modified, or deleted). Skips junk dirs like snapshot(),
    plus run ARTIFACTS the verifier itself creates in work/ (pytest-cov's
    SQLite .coverage, cache dirs) — they are not the agent's edit and must
    never reach the diff / files_touched / git output."""
    skip = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            ".tox", ".egg-info", ".git", ".hypothesis", ".cache"}
    skip_files = {".coverage", ".coverage.*"}
    pristine, work = Path(pristine_dir), Path(work_dir)

    def scan(root: Path) -> Dict[str, Path]:
        out: Dict[str, Path] = {}
        if not root.exists():
            return out
        for p in root.rglob("*"):
            if p.is_dir():
                continue
            rel = p.relative_to(root).as_posix()
            parts = rel.split("/")
            if any(part in skip for part in parts):
                continue
            if p.name in skip_files or p.name == ".coverage":
                continue
            out[rel] = p
        return out

    p_files, w_files = scan(pristine), scan(work)
    changed = []
    for rel in sorted(set(p_files) | set(w_files)):
        if rel not in p_files:
            changed.append(rel)
        elif rel not in w_files:
            changed.append(rel)
        else:
            if p_files[rel].read_bytes() != w_files[rel].read_bytes():
                changed.append(rel)
    return changed


def unified_diff(pristine_dir: str, work_dir: str, max_bytes: int = 100_000) -> Optional[str]:
    """Unified diff of all textual changes between pristine and working
    copies; '' when nothing changed; truncated at max_bytes. Returns None
    if a changed file looks binary (harness reports the file instead).

    Binary detection covers BOTH the NUL byte and non-UTF-8 bytes: the
    strict decode itself must not raise (a run artifact like pytest-cov's
    SQLite .coverage can appear in work/ as a "changed file"; the diff
    must report it as binary, never crash the task on the success path).
    """
    diffs: List[str] = []
    for rel in changed_files(pristine_dir, work_dir):
        p_path = Path(pristine_dir, rel)
        w_path = Path(work_dir, rel)
        try:
            p_text = p_path.read_text(encoding="utf-8", errors="strict") if p_path.exists() else ""
            w_text = w_path.read_text(encoding="utf-8", errors="strict") if w_path.exists() else ""
        except UnicodeDecodeError:
            return None
        # Binary heuristic: NUL byte in either side.
        if "\x00" in p_text or "\x00" in w_text:
            return None
        p_lines = (p_text if p_path.exists() else "").splitlines(keepends=True)
        w_lines = (w_text if w_path.exists() else "").splitlines(keepends=True)
        d = difflib.unified_diff(
            p_lines, w_lines,
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        )
        joined = "".join(d)
        if joined:
            diffs.append(joined)
    out = "".join(diffs)
    if len(out) > max_bytes:
        out = out[:max_bytes] + "\n[diff truncated]\n"
    return out


def is_protected(rel_path: str, protected_patterns: List[str]) -> bool:
    """True if rel_path matches any protected glob (fnmatch against the
    full posix path, the basename, and each directory component)."""
    rel = rel_path.replace("\\", "/")
    for pat in protected_patterns or []:
        if fnmatch.fnmatch(rel, pat):
            return True
        if fnmatch.fnmatch(rel.split("/")[-1], pat):
            return True
        for part in rel.split("/")[:-1]:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def syntax_check(work_dir: str, rel_paths: List[str]) -> Tuple[bool, str]:
    """Compile-check the given changed .py files in the working copy.

    Returns (ok, message): ok=False when any changed .py file has a syntax
    error, with the offending file + error in the message. Non-Python
    files are ignored (assumes a Python repo for Phase 1 per tech lock).
    """
    for rel in rel_paths:
        if not rel.endswith(".py"):
            continue
        p = Path(work_dir, rel)
        if not p.exists():  # deleted file — nothing to check
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            compile(src, rel, "exec")
        except SyntaxError as e:
            return False, f"syntax error in {rel}: {e}"
    return True, ""


def check_edits(
    pristine_dir: str,
    work_dir: str,
    protected_patterns: List[str],
) -> Tuple[bool, str, List[str]]:
    """Full pre-verify validation of the working copy vs pristine.

    Returns (ok, message, changed):
    - ok=False, message explains (protected path hit / syntax error)
    - changed is the repo-relative list of changed files either way.
    """
    changed = changed_files(pristine_dir, work_dir)
    for rel in changed:
        if is_protected(rel, protected_patterns):
            return False, f"protected path modified: {rel}", changed
    ok, msg = syntax_check(work_dir, changed)
    if not ok:
        return False, msg, changed
    return True, "ok", changed


def restore_dir(pristine_dir: str, work_dir: str) -> None:
    """Reset the working copy to pristine (wipe + re-copy). Used between
    attempts and on rollback. Assumes pristine_dir still exists."""
    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.copytree(
        pristine_dir, work_dir,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".git"),
    )
