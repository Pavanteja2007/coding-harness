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
from typing import Dict, List, Optional, Set, Tuple


def snapshot(src: str, dst: str) -> None:
    """Copy a repo tree to dst (the agent's pristine reference copy).

    Assumes src is a directory; skips caches/logs so the diff stays clean.

    dst-inside-src guard: in the plain-`vex` interactive flow (cd <repo>;
    vex) the log root defaults to ./logs UNDER the repo, so dst
    (logs/{task_id}/pristine) lives inside src. A plain copytree then
    descends into its own destination and recurses until RecursionError
    (found live via the interactive no-args drive; scripted callers
    always placed logs outside the repo, so the shape was untested).
    When dst's parent chain runs through src, the top chain segment is
    excluded from the copy — harness artifacts never belong in the
    pristine reference anyway, and the exclusion holds for every
    log-root source (interactive default, --log-root, env override).
    """
    src_p, dst_p = Path(src), Path(dst)
    skip = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".egg-info",
    }
    base_ignore = shutil.ignore_patterns(*skip, "*.pyc", ".git")

    exclude: Optional[str] = None
    try:
        src_r = src_p.resolve()
        dst_parent_r = dst_p.parent.resolve()
        if dst_parent_r == src_r:
            exclude = dst_p.name  # dst directly under src
        elif dst_parent_r.is_relative_to(src_r):
            exclude = dst_parent_r.relative_to(src_r).parts[0]
    except (OSError, ValueError):
        exclude = None  # unresolvable paths: plain behavior

    if exclude is None:
        shutil.copytree(src_p, dst_p, ignore=base_ignore)
        return

    def ignore(path: str, names: List[str]) -> List[str]:
        ignored = set(base_ignore(path, names))
        try:
            if Path(path).resolve() == src_r:
                ignored.add(exclude)
        except OSError:
            pass
        return list(ignored)

    shutil.copytree(src_p, dst_p, ignore=ignore)


def changed_files(pristine_dir: str, work_dir: str) -> List[str]:
    """Repo-relative posix paths that differ between pristine and working
    copies (added, modified, or deleted). Skips junk dirs like snapshot(),
    plus run ARTIFACTS the verifier itself creates in work/ (pytest-cov's
    SQLite .coverage, cache dirs) — they are not the agent's edit and must
    never reach the diff / files_touched / git output."""
    skip = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".egg-info",
        ".git",
        ".hypothesis",
        ".cache",
    }
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


def unified_diff(
    pristine_dir: str, work_dir: str, max_bytes: int = 100_000
) -> Optional[str]:
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
            p_text = (
                p_path.read_text(encoding="utf-8", errors="strict")
                if p_path.exists()
                else ""
            )
            w_text = (
                w_path.read_text(encoding="utf-8", errors="strict")
                if w_path.exists()
                else ""
            )
        except UnicodeDecodeError:
            return None
        # Binary heuristic: NUL byte in either side.
        if "\x00" in p_text or "\x00" in w_text:
            return None
        p_lines = (p_text if p_path.exists() else "").splitlines(keepends=True)
        w_lines = (w_text if w_path.exists() else "").splitlines(keepends=True)
        d = difflib.unified_diff(
            p_lines,
            w_lines,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        joined = "".join(d)
        if joined:
            diffs.append(joined)
    out = "".join(diffs)
    if len(out) > max_bytes:
        out = out[:max_bytes] + "\n[diff truncated]\n"
    return out


# Always-protected VCS/integrity paths (Round 6 adversarial hardening):
# independent of the task's protected_paths config. The agent works in a
# snapshot that drops .git/, so any .git/... path that DOES appear in a
# diff is an agent-created forgery of VCS state — refused outright.
_ALWAYS_PROTECTED = (".git", ".hg", ".svn")


def _normalize_rel(rel_path: str) -> str:
    """Collapse traversal components ('..') out of a repo-relative path.

    'subdir/../../tests/t.py' -> 'tests/t.py' — so a traversal-shaped path
    cannot evade a protected-path glob by prefixing '..' segments. Paths
    that escape the repo entirely ('../../etc/passwd') normalize to their
    tail ('etc/passwd'), which still matches directory-level globs. Pure
    defense-in-depth: the real pipeline feeds rglob-normalized paths that
    cannot contain '..' in the first place.
    """
    rel = rel_path.replace("\\", "/")
    parts: List[str] = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def is_protected(rel_path: str, protected_patterns: List[str]) -> bool:
    """True if rel_path matches any protected glob (fnmatch against the
    full posix path, the basename, and each directory component).

    Round 6 adversarial hardening: the path is '..'-normalized first (a
    traversal form cannot evade the globs), and VCS dirs (.git/.hg/.svn)
    are ALWAYS protected regardless of configuration.
    """
    rel = _normalize_rel(rel_path)
    parts = rel.split("/")
    if (
        any(part in _ALWAYS_PROTECTED for part in parts[:-1])
        or parts[-1] in _ALWAYS_PROTECTED
    ):
        return True
    for pat in protected_patterns or []:
        if fnmatch.fnmatch(rel, pat):
            return True
        if fnmatch.fnmatch(parts[-1], pat):
            return True
        for part in parts[:-1]:
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

    Round 6 adversarial hardening: always-protected VCS dirs (.git/.hg/
    .svn) are checked on a SEPARATE work-tree scan, not just the diff set
    — changed_files deliberately skips .git content (it's agent-forged
    junk in a snapshot that dropped .git), so a diff-only check would
    never see it. An agent-created .git/config must still be refused.
    """
    changed = changed_files(pristine_dir, work_dir)
    for rel in changed:
        if is_protected(rel, protected_patterns):
            return False, f"protected path modified: {rel}", changed
    forged = _forged_vcs_paths(work_dir)
    if forged:
        return False, f"protected path modified: {forged[0]}", changed
    ok, msg = syntax_check(work_dir, changed)
    if not ok:
        return False, msg, changed
    return True, "ok", changed


def _forged_vcs_paths(work_dir: str) -> List[str]:
    """Repo-relative paths under any .git/.hg/.svn dir in work/ that do NOT
    exist in pristine (the snapshot drops VCS dirs, so any that appear in
    work/ were created by the agent). Never raises.

    pathlib's rglob('.git/**') yields the dir itself but not its files on
    some versions/hosts — the reliable form is rglob over each VCS dir's
    CONTENTS via a plain rglob('*') filtered by the VCS path components.
    """
    out: List[str] = []
    try:
        root = Path(work_dir)
        if not root.is_dir():
            return out
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            parts = p.relative_to(root).as_posix().split("/")
            if any(part in _ALWAYS_PROTECTED for part in parts[:-1]):
                out.append(p.relative_to(root).as_posix())
                if len(out) >= 20:
                    return out
    except OSError:
        pass
    return out


def restore_dir(pristine_dir: str, work_dir: str) -> None:
    """Reset the working copy to pristine (wipe + re-copy). Used between
    attempts and on rollback. Assumes pristine_dir still exists."""
    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.copytree(
        pristine_dir,
        work_dir,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".git"),
    )


# --- Coordinated multi-file groups (Improvement Round 2, Task B) ------------

_GROUP_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.pyc",
    ".git",
    ".hypothesis",
    ".cache",
    ".coverage",
    ".coverage.*",
)


def restore_group(
    pristine_dir: str,
    work_dir: str,
    group_files: List[str],
) -> List[str]:
    """Roll back ONE coordinated-change group as an atomic unit.

    Every file in group_files is restored to its pristine state — the
    whole coordinated change reverts together, not just one file. Files
    OUTSIDE the group (and run artifacts the group files might sit
    next to) are untouched. Assumes pristine_dir exists and group_files
    are repo-relative posix paths normalized like the rest of the
    editor (backslashes accepted). Returns the list of restored paths
    (normalized); a group file missing from BOTH trees (e.g. deleted
    everywhere) contributes nothing and never raises.
    """
    restored: List[str] = []
    for rel in group_files or []:
        norm = rel.replace("\\", "/")
        norm = _normalize_rel(norm)
        if not norm:
            continue
        src = Path(pristine_dir, norm)
        dst = Path(work_dir, norm)
        try:
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif dst.exists():
                # File existed only in work/ (agent-created group member
                # like a new module): its pristine state is "absent".
                dst.unlink()
            else:
                continue
            restored.append(norm)
        except OSError:
            continue  # best-effort rollback; the attempt gate re-checks
    return restored


def group_orphans(
    work_dir: str,
    group_files: List[str],
) -> List[str]:
    """Empty directories left in work/ after a group rollback (dirs that
    held only group files and are now empty). The atomic rollback uses
    this to leave the tree exactly as it found it. Assumes group_files
    are repo-relative posix paths; returns the dirs removed (sorted)."""
    out: List[str] = []
    candidates: Set[Path] = set()
    for rel in group_files or []:
        p = Path(work_dir, rel.replace("\\", "/"))
        if p.parent != Path(work_dir):
            candidates.add(p.parent)
    for d in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                out.append(d.relative_to(work_dir).as_posix())
        except OSError:
            continue
    return out
