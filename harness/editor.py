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

R2-03 adds the TEST-CONFIGURATION layer: a repo's test *configuration*
(conftest.py, pytest.ini, the pytest tables of pyproject.toml / setup.cfg /
tox.ini, sitecustomize.py, and the JS/PHP/... runner configs) is as much a
part of the contract as its test files, because relaxing it makes the
regression gate report a clean run for a suite that is no longer the one the
baseline ran. Whole-file surfaces are protected in `is_protected`;
table-scoped ones and the effective-configuration comparison are enforced in
`check_edits` through `harness.test_config` (resolution, the declared-intent
exception, and the `test_config_changed` receipt all live there).

R2-06 adds the ONE exact-block edit primitive (`apply_text_edit`) that both the
legacy `EDIT` verb and the typed `edit` tool are meant to speak: it detects the
file's newline convention and encoding, REFUSES an ambiguous target instead of
silently taking the first match, writes through the existing atomic-write
primitive, and rolls the file back byte-for-byte when the post-edit
syntax/validation gate fails. Its refusal vocabulary is the kernel's
(`ambiguous_match` / `no_match` / `stale_read`) so both paths speak one
language rather than two dialects. See `EditSession`, `detect_encoding`,
`detect_newline` and `apply_text_edit` at the end of this module.
"""

import codecs
import difflib
import fnmatch
import hashlib
import re
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from harness.redaction import redact_text_for_journal


class EditorConflictError(RuntimeError):
    """Raised when rollback would overwrite a newer user change."""


def snapshot(src: str, dst: str) -> None:
    """Copy a repo tree to dst (the agent's pristine reference copy).

    Assumes src is a directory; skips caches/logs so the diff stays clean.

    dst-inside-src guard: in the plain-`neo` interactive flow (cd <repo>;
    neo) the log root defaults to ./logs UNDER the repo, so dst
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

    def safe_ignore(path: str, names: List[str]) -> List[str]:
        ignored = set(base_ignore(path, names))
        for name in names:
            try:
                if (Path(path) / name).is_symlink():
                    ignored.add(name)
            except OSError:
                ignored.add(name)
        return list(ignored)

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
        shutil.copytree(src_p, dst_p, ignore=safe_ignore)
        return

    def ignore(path: str, names: List[str]) -> List[str]:
        ignored = set(safe_ignore(path, names))
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
    skip_files = {".coverage"}
    pristine, work = Path(pristine_dir), Path(work_dir)

    def scan(root: Path) -> Dict[str, Path]:
        out: Dict[str, Path] = {}
        if not root.exists():
            return out
        for p in root.rglob("*"):
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                out[rel] = p
                continue
            if p.is_dir():
                continue
            parts = rel.split("/")
            if any(part in skip for part in parts):
                continue
            if p.name in skip_files or p.name.startswith(".coverage."):
                continue
            out[rel] = p
        return out

    p_files, w_files = scan(pristine), scan(work)
    changed = []
    for rel in sorted(set(p_files) | set(w_files)):
        if rel not in p_files or rel not in w_files:
            changed.append(rel)
        else:
            p_is_link = p_files[rel].is_symlink()
            w_is_link = w_files[rel].is_symlink()
            if p_is_link or w_is_link:
                if p_is_link and w_is_link:
                    try:
                        same_link = p_files[rel].readlink() == w_files[rel].readlink()
                    except OSError:
                        same_link = False
                    if not same_link:
                        changed.append(rel)
                else:
                    changed.append(rel)
            elif p_files[rel].read_bytes() != w_files[rel].read_bytes():
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
        if p_path.is_symlink() or w_path.is_symlink():
            return None
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


# --- R2-03: test-configuration protection ------------------------------------
# A repo's test CONFIGURATION is as much a part of the contract as its test
# files: an agent that adds `-k`, `--ignore`, `--deselect` or
# `-p no:<plugin>` to addopts, or swaps the inifile, otherwise makes the
# regression gate report a clean run for a suite that no longer is the one
# the baseline ran. The surface table, the declared-intent rule and the
# effective-configuration resolver live in `harness.test_config`; these
# helpers are the editor's re-exports so callers already importing the editor
# do not need a second module.


def _is_whole_file_test_config(rel_path: str) -> bool:
    """True when the whole file is a test-config surface (no table scoping).

    Assumes `rel_path` is repo-relative; normalization happens inside the
    classifier. Never raises: an unclassifiable path is not protected here.
    """
    from harness.test_config import WHOLE_FILE, classify_test_config_path

    try:
        surface = classify_test_config_path(rel_path)
    except Exception:
        return False
    return surface is not None and surface.scope == WHOLE_FILE


def test_config_patterns(language: Optional[str] = None) -> List[str]:
    """Glob patterns for the whole-file test-config surfaces of a language.

    Assumes nothing. Intended for a consumer that wants to EXTEND an existing
    `config["protected_paths"]` list (the CLI does this) rather than fork a
    second policy; the edit gate enforces the table-scoped surfaces and the
    declared-intent exception itself.
    """
    from harness.test_config import test_config_patterns as _patterns

    return _patterns(language)


def extended_protected_patterns(
    protected_patterns: Optional[List[str]] = None,
    *,
    language: Optional[str] = None,
    ecosystem: Any = None,
) -> List[str]:
    """`protected_paths` plus the whole-file test-config surfaces.

    Assumes `protected_patterns` is a list of fnmatch globs (possibly None).
    Order-stable and duplicate-free, so a caller can use the result as its
    `protected_paths` without changing any existing decision.

    R2-12 adds ONE additive, keyword-only `ecosystem` parameter. It accepts an
    `execution.ecosystems.Ecosystem` or a registered ecosystem NAME, and adds
    that language's test globs (`*_test.go` for Go, `src/test/*` for Java, ...)
    on top of the caller's list. That is what makes the protected set
    LANGUAGE-CORRECT: `DEFAULTS["protected_paths"]` is Python-shaped and matches
    no Java or Go test file at all, so without this an agent in a Maven or Go
    repository had no protected test surface whatsoever.

    The parameter is keyword-only with a None default and the import is guarded
    and lazy, so every existing three-positional-argument call site is
    byte-identical and this file stays importable without `execution`. The
    caller's own globs are KEPT, never replaced: the operator's list is policy.
    """
    out: List[str] = [str(item) for item in (protected_patterns or [])]
    if ecosystem is not None:
        for glob in _ecosystem_protected_globs(ecosystem):
            if glob not in out:
                out.append(glob)
    for pattern in test_config_patterns(language):
        if pattern not in out:
            out.append(pattern)
    return out


def _ecosystem_protected_globs(ecosystem: Any) -> List[str]:
    """Resolve `ecosystem` to its test globs, or [] when it cannot be resolved.

    Assumes `ecosystem` is an `Ecosystem` or a registered name. The registry is
    imported inside a guard: an unavailable or broken registry degrades to "no
    extra globs" (the caller's list still applies) rather than raising inside a
    policy function, and an unknown NAME is not silently treated as the
    caller's own patterns.
    """
    if isinstance(ecosystem, str):
        try:
            from execution.ecosystems import ecosystem as _registry_lookup
        except Exception:
            return []
        try:
            resolved = _registry_lookup(ecosystem.strip().lower())
        except Exception:
            return []
        return list(resolved.protected_globs) if resolved is not None else []
    globs = getattr(ecosystem, "protected_globs", None)
    if not globs:
        return []
    try:
        return [str(glob) for glob in globs]
    except TypeError:
        return []


def declared_test_config_change(config: Optional[Dict[str, Any]]) -> Optional[str]:
    """The declared reason a task may change test configuration, or None.

    Assumes `config` is a merged `Task.config` mapping. Re-exported from
    `harness.test_config` so the edit gate, the policy layer, and the CLI all
    read the SAME key (`test_config_change`) and the same not-declared rules.
    """
    from harness.test_config import declared_test_config_change as _declared

    return _declared(config)


def is_protected(rel_path: str, protected_patterns: List[str]) -> bool:
    """True if rel_path matches any protected glob (fnmatch against the
    full posix path, the basename, and each directory component).

    Round 6 adversarial hardening: the path is '..'-normalized first (a
    traversal form cannot evade the globs), and VCS dirs (.git/.hg/.svn)
    are ALWAYS protected regardless of configuration.

    R2-03: a repository's test *configuration* is now part of the contract,
    so a WHOLE-FILE test-config surface (conftest.py, pytest.ini,
    sitecustomize.py, the JS/PHP/... runner configs) is always protected here
    too, independent of `protected_paths`. TABLE-SCOPED surfaces are NOT
    decided here — pyproject.toml / package.json / setup.cfg / tox.ini /
    angular.json are dual-purpose (adding a dependency is a normal edit) and
    only their test tables are protected, which needs the tree, not a path.
    That check lives in `check_edits` via `harness.test_config`.
    """
    from harness.test_config import is_test_config_path

    rel = _normalize_rel(rel_path)
    parts = rel.split("/")
    lowered = [part.lower() for part in parts]
    if (
        any(part in _ALWAYS_PROTECTED for part in lowered[:-1])
        or lowered[-1] in _ALWAYS_PROTECTED
    ):
        return True
    if is_test_config_path(rel) and _is_whole_file_test_config(rel):
        return True
    for pat in protected_patterns or []:
        pattern = str(pat).lower()
        if fnmatch.fnmatch(rel.lower(), pattern):
            return True
        if fnmatch.fnmatch(lowered[-1], pattern):
            return True
        for part in lowered[:-1]:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


def syntax_check(
    work_dir: str, rel_paths: List[str], *, encoding: Optional[str] = None
) -> Tuple[bool, str]:
    """Syntax-check the given changed source files in the working copy.

    Returns (ok, message): ok=False when any changed file has a syntax
    error, with the offending file + error in the message. Language-aware
    (Python AND JS/TS): .py via compile() (unchanged); .js/.jsx/.mjs/
    .cjs/.ts/.tsx via tree-sitter (harness.lint.check_syntax — same
    parsers the code graph uses; offline, no host node needed). Files of
    other extensions are ignored.

    `encoding` is ADDITIVE and keyword-only: it names the codec to read a
    file with, and None keeps the historical `utf-8` / `errors="replace"`
    read byte-for-byte, so every existing call site is unchanged.
    R2-06's `apply_text_edit` passes the encoding it already detected, which
    is what makes a UTF-8-BOM or latin-1 Python file checkable at all: read
    as plain `utf-8`, a BOM decodes to U+FEFF and `compile()` reports "invalid
    non-printable character" for a file that is perfectly valid - a false
    positive that would have rolled back every edit to such a file.
    """
    from harness.lint import LintFinding
    from harness.lint import check_syntax as _lint_syntax

    for rel in rel_paths:
        raw = str(rel).replace("\\", "/")
        raw_path = Path(raw)
        if raw_path.is_absolute() or ".." in raw_path.parts:
            return False, f"unsafe path refused: {rel}"
        p = Path(work_dir, raw_path)
        if p.is_symlink():
            return False, f"symbolic link is not an editable source file: {rel}"
        if not p.exists():  # deleted file — nothing to check
            continue
        if not rel.endswith((".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")):
            continue
        try:
            if encoding:
                src = p.read_text(encoding=encoding, errors="strict")
            else:
                src = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as e:
            return False, f"unreadable during syntax check ({rel}): {e}"
        findings: List[LintFinding] = _lint_syntax(src, rel)
        if findings:
            f = findings[0]
            return False, f"syntax error in {rel}: {f.message}"
    return True, ""


def precommit_check(
    candidate_src: str,
    relative_path: str,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> "Any":
    """Check candidate content BEFORE it is written; returns a lint `EditCheck`.

    AGT-03. Assumes `candidate_src` is the exact post-edit text and
    `relative_path` is the repo-relative path it will be written to. This is
    the one seam every mutating path can share: the check needs the content,
    not a file, so it is a pure function of a string and cannot be bypassed by
    forgetting to re-read from disk.

    The tri-state is the point. `passed` is the only status that means a
    checker ran; `unchecked` (unsupported language, missing tree-sitter
    grammar, non-text content) and `disabled` (a caller turned it off) both
    carry a reason, because a silent skip is indistinguishable from a pass to
    whoever reads the tool result. `apply_text_edit` appends the non-pass
    statuses to its own message so the model is told, rather than left to
    assume its edit was checked.

    A check that RAISES is reported as `failed`: a crashing guard is a failed
    guard, which is the same rule the post-write `validate` gate has always
    used.

    Config keys, read through `_config_value` so an absent key takes the
    internal default (see the module docstring — no key here is in
    `harness/config.py::DEFAULTS`, because a default is merged into every task
    and every eval arm):
      edit_inline_lint            (default True; only an explicit False is off)
      edit_inline_lint_names      (default False — see harness.lint's docstring)
      edit_inline_lint_context_lines (default 3)
    """
    from harness.lint import (
        CHECK_DISABLED,
        CHECK_FAILED,
        EditCheck,
        check_source_for_edit,
    )

    path = str(relative_path or "")
    enabled = _config_value(config, "edit_inline_lint", True)
    if enabled is False:
        return EditCheck(
            CHECK_DISABLED,
            path,
            kind="disabled",
            reason="the in-edit lint is off for this run "
            "(edit_inline_lint=False); the content was NOT checked",
        )
    check_names = bool(_config_value(config, "edit_inline_lint_names", False))
    span = _config_value(config, "edit_inline_lint_context_lines", 3)
    try:
        span = max(0, int(span))
    except (TypeError, ValueError):
        span = 3
    try:
        return check_source_for_edit(
            candidate_src, path, check_names=check_names, context_lines=span
        )
    except Exception as exc:  # a crashing check is a failed check
        return EditCheck(
            CHECK_FAILED,
            path,
            kind="lint_error",
            message=f"the in-edit check raised before the write: "
            f"{type(exc).__name__}: {exc}",
            reason="an internal check error is treated as a failure, never as a pass",
        )


def check_edits(
    pristine_dir: str,
    work_dir: str,
    protected_patterns: List[str],
    *,
    config: Optional[Dict[str, Any]] = None,
    trace: Optional[Any] = None,
    report: Optional[List[Any]] = None,
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

    R2-03 (all three params are ADDITIVE and keyword-only, so every existing
    three-positional-arg call site is byte-identical):
    - `config` is the merged Task.config. It is the only place a legitimate
      test-configuration change can be DECLARED (`test_config_change`), and
      the declaration is recorded rather than silently honored.
    - `trace` is an optional sink with a `.log(kind, data)` method; the
      effective-configuration receipt is written to it as `test_config`, and
      as `test_config_changed` whenever the resolution differs from baseline.
    - `report` is an optional out-list the receipt dict is appended to.
    Omitting all three keeps the pre-R2-03 behaviour plus the refusal.

    AGT-03: this stays the BACKSTOP and is deliberately NOT replaced by the
    in-edit check. `precommit_check` refuses a broken mutation in
    milliseconds, at the tool boundary, before anything is written; this runs
    over every changed file at the pre-verify checkpoint and is the only one of
    the two that sees an edit some other path made (a shell redirect, a tool
    that bypassed the editor, a mutation applied while the in-edit check was
    off). Both are required: the in-edit guard is a guard, this is the audit.
    """
    changed = changed_files(pristine_dir, work_dir)
    for rel in changed:
        if Path(pristine_dir, rel).is_symlink() or Path(work_dir, rel).is_symlink():
            return False, f"symbolic link is not an editable path: {rel}", changed
    # R2-03: the test-configuration gate runs BEFORE the configured-glob loop
    # so its message names the real reason, and so a DECLARED change is not
    # then refused a second time by the generic protected-path rule.
    tc_ok, tc_message, tc_allowed = _test_config_gate(
        pristine_dir, work_dir, changed, config=config, trace=trace, report=report
    )
    if not tc_ok:
        return False, tc_message, changed
    allowed = set(tc_allowed)
    for rel in changed:
        if rel in allowed:
            continue
        if is_protected(rel, protected_patterns):
            return False, f"protected path modified: {rel}", changed
    forged = _forged_vcs_paths(work_dir)
    if forged:
        return False, f"protected path modified: {forged[0]}", changed
    ok, msg = syntax_check(work_dir, changed)
    if not ok:
        return False, msg, changed
    return True, "ok", changed


def _test_config_gate(
    pristine_dir: str,
    work_dir: str,
    changed: List[str],
    *,
    config: Optional[Dict[str, Any]] = None,
    trace: Optional[Any] = None,
    report: Optional[List[Any]] = None,
) -> Tuple[bool, str, List[str]]:
    """Run the R2-03 test-configuration guard and record its receipt.

    Assumes `changed` is the repo-relative diff of the two trees. Returns
    (ok, message, allowed_paths) using the editor's existing vocabulary so a
    caller's assertions on "protected path modified" keep working;
    `allowed_paths` are the surface files a declared change cleared.

    Two failure policies, deliberately different:
    - a guard that RAISES degrades to "not blocking" plus a recorded
      `test_config_guard_failed` row, because an unrelated OSError must not
      turn a correct fix into a failure. The receipt says the guard did not
      run, so a reader is never told "no test-config change" on the strength
      of a crash.
    - a guard that RESOLVES nothing (unreadable tree) still refuses a
      file-level violation, because that verdict needs no resolution. An
      unresolved resolution with no violation passes with
      `resolved: false` in the receipt.
    """
    try:
        from harness.test_config import test_config_guard

        verdict = test_config_guard(
            pristine_dir, work_dir, config=config, changed_paths=changed
        )
    except Exception as exc:  # pragma: no cover - defensive; see docstring
        if trace is not None and hasattr(trace, "log"):
            try:
                trace.log("test_config_guard_failed", {"error": str(exc)})
            except Exception:
                pass
        return True, "ok", []
    record = verdict.receipt.to_record()
    if report is not None:
        try:
            report.append(record)
        except Exception:
            pass
    if trace is not None and hasattr(trace, "log"):
        try:
            trace.log("test_config", record)
            if verdict.receipt.changed:
                trace.log("test_config_changed", record)
        except Exception:
            pass
    return verdict.ok, verdict.message, list(verdict.allowed_paths)


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
            parts = p.relative_to(root).as_posix().split("/")
            if not any(part.lower() in _ALWAYS_PROTECTED for part in parts):
                continue
            if p.is_file() or p.is_dir():
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
    base_ignore = shutil.ignore_patterns(
        "__pycache__", ".pytest_cache", "*.pyc", ".git"
    )

    def ignore(path: str, names: List[str]) -> List[str]:
        ignored = set(base_ignore(path, names))
        for name in names:
            try:
                if (Path(path) / name).is_symlink():
                    ignored.add(name)
            except OSError:
                ignored.add(name)
        return list(ignored)

    shutil.copytree(pristine_dir, work_dir, ignore=ignore)


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
    pristine_root = Path(pristine_dir).resolve()
    work_root = Path(work_dir).resolve()
    for rel in group_files or []:
        raw = str(rel).replace("\\", "/")
        if Path(raw).is_absolute() or ".." in Path(raw).parts:
            continue
        norm = _normalize_rel(raw)
        if not norm or Path(norm).is_absolute() or ".." in Path(norm).parts:
            continue
        try:
            src = (pristine_root / norm).resolve()
            dst = (work_root / norm).resolve(strict=False)
            src.relative_to(pristine_root)
            dst.relative_to(work_root)
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            if src.is_file() and not src.is_symlink():
                if dst.is_symlink():
                    dst.unlink()
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif dst.exists() or dst.is_symlink():
                if dst.is_dir() and not dst.is_symlink():
                    continue
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
    root = Path(work_dir).resolve()
    for rel in group_files or []:
        raw = str(rel).replace("\\", "/")
        if Path(raw).is_absolute() or ".." in Path(raw).parts:
            continue
        norm = _normalize_rel(raw)
        if not norm or Path(norm).is_absolute() or ".." in Path(norm).parts:
            continue
        try:
            p = (root / norm).resolve(strict=False)
            p.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if p.parent != root:
            candidates.add(p.parent)
    for d in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                out.append(d.relative_to(work_dir).as_posix())
        except OSError:
            continue
    return out


def safe_edit(
    repo_path: str,
    relative_path: str,
    old_string: str,
    new_string: str,
    *,
    state_dir: Optional[str] = None,
    expected_hash: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    protected_patterns: Optional[List[str]] = None,
    hunk_id: Optional[str] = None,
) -> Any:
    """Apply one unique, revision-checked edit through the workspace journal."""
    from execution.workspace import apply_exact_edit

    _stage_note([relative_path], done=False)
    result = apply_exact_edit(
        repo_path,
        relative_path,
        old_string,
        new_string,
        state_dir=state_dir,
        expected_hash=expected_hash,
        expected_sha256=expected_sha256,
        protected_paths=protected_patterns or [],
        hunk_id=hunk_id,
    )
    _stage_note([relative_path], done=True)
    return result


exact_edit = safe_edit
edit_file = safe_edit


def safe_write(
    repo_path: str,
    relative_path: str,
    content: str,
    *,
    state_dir: Optional[str] = None,
    expected_hash: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    overwrite: bool = False,
    protected_patterns: Optional[List[str]] = None,
) -> Any:
    """Create or replace text through the atomic workspace mutation backend."""
    from execution.workspace import Workspace

    _stage_note([relative_path], done=False)
    workspace = Workspace(
        repo_path, state_dir, protected_paths=protected_patterns or []
    )
    try:
        result = workspace.write_file(
            relative_path,
            content,
            overwrite=overwrite,
            expected_hash=expected_hash,
            expected_sha256=expected_sha256,
        )
    finally:
        workspace.close()
    _stage_note([relative_path], done=True)
    return result


atomic_write = safe_write


def safe_rename(
    repo_path: str,
    source_path: str,
    destination_path: str,
    *,
    state_dir: Optional[str] = None,
    expected_hash: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    protected_patterns: Optional[List[str]] = None,
    hunk_id: Optional[str] = None,
) -> Any:
    """Rename one file through the journaled workspace mutation backend."""
    from execution.workspace import Workspace

    _stage_note([source_path, destination_path], done=False)
    workspace = Workspace(
        repo_path,
        state_dir,
        protected_paths=protected_patterns or [],
    )
    try:
        result = workspace.rename_file(
            source_path,
            destination_path,
            expected_hash=expected_hash,
            expected_sha256=expected_sha256,
            hunk_id=hunk_id,
        )
    finally:
        workspace.close()
    _stage_note([source_path, destination_path], done=True)
    return result


def safe_delete(
    repo_path: str,
    relative_path: str,
    *,
    state_dir: Optional[str] = None,
    expected_hash: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    protected_patterns: Optional[List[str]] = None,
    hunk_id: Optional[str] = None,
) -> Any:
    """Delete one file with a mandatory revision precondition and durable undo."""
    from execution.workspace import Workspace

    _stage_note([relative_path], done=False)
    workspace = Workspace(
        repo_path,
        state_dir,
        protected_paths=protected_patterns or [],
    )
    try:
        result = workspace.delete_file(
            relative_path,
            expected_hash=expected_hash,
            expected_sha256=expected_sha256,
            hunk_id=hunk_id,
        )
    finally:
        workspace.close()
    _stage_note([relative_path], done=True)
    return result


def safe_apply_patch(
    repo_path: str,
    patch: str,
    *,
    state_dir: Optional[str] = None,
    expected_revisions: Optional[Dict[str, Any]] = None,
    protected_patterns: Optional[List[str]] = None,
    allow_delete: bool = True,
) -> Any:
    """Apply a multi-file unified patch with per-file revision preconditions."""
    from execution.workspace import Workspace

    if expected_revisions is None:
        raise EditorConflictError("safe_apply_patch requires expected_revisions")
    patch_paths = sorted(
        {
            str(line.split(" b/", 1)[1].split("\t", 1)[0]).strip()
            for line in str(patch or "").splitlines()
            if line.startswith("+++ b/")
        }
        or set(expected_revisions)
    )
    _stage_note(patch_paths, done=False)
    workspace = Workspace(
        repo_path,
        state_dir,
        protected_paths=protected_patterns or [],
    )
    try:
        result = workspace.apply_unified_patch(
            patch,
            expected_revisions=expected_revisions,
            allow_delete=allow_delete,
        )
    finally:
        workspace.close()
    _stage_note(patch_paths, done=True)
    return result


def conflict_safe_restore_group(
    pristine_dir: str,
    work_dir: str,
    group_files: List[str],
    expected_hashes: Dict[str, Optional[str]],
) -> List[str]:
    """Restore a coordinated group only when every current hash still matches."""
    pristine_root = Path(pristine_dir).resolve()
    work_root = Path(work_dir).resolve()
    normalized: List[str] = []
    for value in group_files or []:
        raw = str(value).replace("\\", "/")
        if Path(raw).is_absolute() or ".." in Path(raw).parts:
            raise EditorConflictError(f"unsafe rollback path: {value}")
        relative = _normalize_rel(raw)
        if not relative:
            raise EditorConflictError(f"unsafe rollback path: {value}")
        normalized.append(relative)
    for relative in normalized:
        expected = expected_hashes.get(relative)
        current = work_root / relative
        if current.is_symlink():
            raise EditorConflictError(f"rollback target is a symbolic link: {relative}")
        actual = (
            hashlib.sha256(current.read_bytes()).hexdigest()
            if current.is_file()
            else None
        )
        if actual != expected:
            raise EditorConflictError(
                f"rollback target changed after agent edit: {relative}"
            )
        source = pristine_root / relative
        if source.is_symlink():
            raise EditorConflictError(
                f"pristine rollback target is a symbolic link: {relative}"
            )
    return restore_group(pristine_dir, work_dir, normalized)


# --- R2-06: one exact-block edit primitive, one refusal vocabulary -------------
#
# The legacy `EDIT` verb (harness/agent_loop.py) and the typed `edit` tool
# (harness/tools.py -> execution.workspace) used to be two dialects: the legacy
# one took the FIRST match of an ambiguous target, read every file as UTF-8
# with errors="replace" (so a CRLF file lost its newlines and a latin-1 file
# was silently re-encoded), and left a syntactically broken file in the work
# tree when its own post-edit check failed. Everything below exists so there is
# one place that gets all four right, and so a caller cannot accidentally get
# the old behaviour by reaching for `Path.write_text` instead.
#
# The three slugs below are the KERNEL's slugs, and
# `harness/agent_kernel/tools.py` holds the same three literals. They are
# pinned equal by a test (`tests/test_ceiling_r2_06_editing.py`) rather than
# re-imported, because `harness.editor` is the lowest-level of the three
# modules and must stay importable without the kernel or the tool catalog.

#: The target text was not found verbatim. The kernel's slug.
ERROR_NO_MATCH = "no_match"
#: The target text matched more than once; the edit is refused and the
#: candidates are returned. The kernel's slug.
ERROR_AMBIGUOUS_MATCH = "ambiguous_match"
#: The caller quoted (or should have quoted) a content digest that no longer
#: matches, or tried to mutate a file the session never read. The kernel's
#: slug - the kernel's own unbound-digest refusal uses this same value.
ERROR_STALE_READ = "stale_read"

# R2-06 additions to the vocabulary, for conditions the kernel never had a name
# for. They are documented extensions, not replacements: the three slugs above
# are the shared ones and mean exactly what the kernel means by them.
#: The file's encoding could not be determined with certainty. Refused, never
#: guessed - a wrong guess rewrites every non-ASCII byte in the file.
ERROR_UNDETERMINED_ENCODING = "undetermined_encoding"
#: The post-edit syntax/validation gate failed. `rolled_back` on the outcome
#: says whether the file was already restored to its pre-edit bytes.
ERROR_POST_CHECK_FAILED = "post_check_failed"
#: The path is missing, is not a regular file, or is otherwise un-editable
#: (symlink, traversal-shaped, protected, too large). Deliberately NOT one of
#: the match slugs: a file that cannot be read at all is the permission
#: policy's refusal to own, exactly as the kernel decided in VEX-CEILING-04.
ERROR_EDIT_REFUSED = "edit_refused"

#: Every slug `apply_text_edit` can return, for callers that want to assert
#: they are not about to grow a private dialect.
EDIT_ERROR_KINDS: Tuple[str, ...] = (
    ERROR_AMBIGUOUS_MATCH,
    ERROR_NO_MATCH,
    ERROR_STALE_READ,
    ERROR_UNDETERMINED_ENCODING,
    ERROR_POST_CHECK_FAILED,
    ERROR_EDIT_REFUSED,
)

# PEP 263 coding cookie. MULTILINE because the cookie is legal on the first OR
# the second line, and a single-line `^` would only ever find the first.
_CODING_COOKIE = re.compile(
    rb"^[ \t\f]*\#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)", re.MULTILINE
)

# BOM table, longest-signature first: UTF-32-LE starts with the UTF-16-LE BOM
# (FF FE 00 00), so checking UTF-16 first would mis-decode every UTF-32 file.
_BOM_ENCODINGS: Tuple[Tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


@dataclass(frozen=True)
class EditCandidate:
    """One place `old_string` occurs, for an `ambiguous_match` refusal.

    `line` is 1-based and counted in the file's own bytes, so it is the line a
    human sees. `preview` is decoded for DISPLAY with errors="replace" and is
    explicitly not a claim about the file's encoding.
    """

    offset: int
    line: int
    preview: str

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe projection used in receipts and trace rows.

        Redaction boundary, same decision and same authority as
        `EditOutcome.to_dict`: `preview` is real file content, so it is
        redacted here — the one place a candidate becomes a record — rather
        than at each consumer. `offset` and `line` are numbers and are not
        redacted, which is what keeps the ambiguous-match line-number
        assertions exact.
        """
        return {
            "offset": self.offset,
            "line": self.line,
            "preview": redact_text_for_journal(
                self.preview, where="EditCandidate.to_dict.preview"
            ),
        }


@dataclass(frozen=True)
class EditOutcome:
    """The result of one `apply_text_edit` attempt - a receipt, not an exception.

    `ok` is the only success signal; every refusal carries a stable
    `error_kind` from `EDIT_ERROR_KINDS` plus a model-actionable `message`.
    `rolled_back` is the load-bearing honesty field: when it is True the file on
    disk is byte-identical to its pre-edit state, and when the edit failed the
    post-edit check the caller can say so instead of leaving the next turn to
    read the harness's own damage.

    AGT-03 adds the in-edit check's own receipt. `pre_commit` says the
    candidate content was rejected BEFORE anything was written (so
    `rolled_back=True` there means "nothing was ever committed", not "a write
    was undone"), and `check_status` / `check_line` / `check_context` /
    `check_reason` say whether the content was actually checked at all.
    `check_status` is the tri-state from `harness.lint` — a consumer that
    renders "the edit was checked and is clean" must read it, because
    `checked=False` means no checker applied, which is NOT a pass.
    """

    ok: bool
    path: str
    error_kind: str = ""
    message: str = ""
    encoding: str = ""
    newline: str = ""
    newline_mixed: bool = False
    newline_adapted: bool = False
    match_count: int = 0
    replacements: int = 0
    candidates: Tuple[EditCandidate, ...] = ()
    rolled_back: bool = False
    digest_required: bool = False
    digest_relaxed: bool = False
    digest_relaxed_reason: str = ""
    write_mode: str = ""
    pre_sha256: str = ""
    post_sha256: str = ""
    trace_kind: str = ""
    pre_commit: bool = False
    check_status: str = ""
    check_line: int = 0
    check_context: str = ""
    check_reason: str = ""

    def __bool__(self) -> bool:
        """Truthy when the edit was applied; `if outcome:` reads as success."""
        return self.ok

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe receipt, minus the unused `trace_kind` field.

        **Redaction boundary (decision: redact AT THE BOUNDARY, here).** This
        is the ONE place an `EditOutcome` becomes a readable record, and it is
        what `harness/tools.py`, the journal rows (`edit_applied` /
        `edit_rolled_back` / `edit_refused`) and every CLI consumer read. Three
        fields carry real file bytes:

        * ``candidates[].preview`` — real bytes either side of each match
          offset (`_candidates`, editor.py:1322), so an ambiguous edit to a
          `.env` shows the line;
        * ``check_context`` — the `+/-3` lines of real source around a
          pre-commit syntax failure (`harness.lint._context_block`);
        * ``message`` — the refusal prose, which quotes the offending text.

        They are redacted HERE rather than at each consumer, so the journal,
        the tool result and the CLI are safe by construction instead of by
        three separate decisions that can disagree. The redaction is a no-op on
        any value the shared authority does not consider secret-shaped, so the
        byte-exact `pre_sha256` / `post_sha256` / `check_line` /
        `candidate.line` assertions that guard this primitive are unaffected:
        none of those fields passes through the redactor. The authority is
        `shared.security`, reached through `harness.redaction`, and the
        boundary is fail-closed — an unredactable value is REPLACED with
        ``(detail withheld: ...)`` rather than passed through.
        """
        record = {
            "ok": self.ok,
            "path": self.path,
            "error_kind": self.error_kind,
            "message": redact_text_for_journal(
                self.message, where="EditOutcome.to_dict.message"
            ),
            "encoding": self.encoding,
            "newline": _newline_name(self.newline),
            "newline_mixed": self.newline_mixed,
            "newline_adapted": self.newline_adapted,
            "match_count": self.match_count,
            "replacements": self.replacements,
            "candidates": [
                {
                    "line": item.line,
                    "offset": item.offset,
                    "preview": redact_text_for_journal(
                        item.preview,
                        where="EditOutcome.to_dict.candidate.preview",
                    ),
                }
                for item in self.candidates
            ],
            "rolled_back": self.rolled_back,
            "digest_required": self.digest_required,
            "digest_relaxed": self.digest_relaxed,
            "digest_relaxed_reason": redact_text_for_journal(
                self.digest_relaxed_reason,
                where="EditOutcome.to_dict.digest_relaxed_reason",
            ),
            "write_mode": self.write_mode,
            "pre_sha256": self.pre_sha256,
            "post_sha256": self.post_sha256,
            "pre_commit": self.pre_commit,
            "check_status": self.check_status,
            "check_line": self.check_line,
            "check_context": redact_text_for_journal(
                self.check_context, where="EditOutcome.to_dict.check_context"
            ),
            "check_reason": redact_text_for_journal(
                self.check_reason, where="EditOutcome.to_dict.check_reason"
            ),
        }
        return record


def _newline_name(newline: str) -> str:
    """Render a newline sequence as a stable receipt token ('crlf'/'lf'/'cr')."""
    return {"\r\n": "crlf", "\n": "lf", "\r": "cr"}.get(newline, "")


def _log(trace: Optional[Any], kind: str, data: Dict[str, Any]) -> None:
    """Best-effort trace write. A tracing failure must never change an edit."""
    if trace is None or not hasattr(trace, "log"):
        return
    try:
        trace.log(kind, data)
    except Exception:  # pragma: no cover - tracing is never load-bearing
        pass


def detect_encoding(data: bytes) -> Optional[str]:
    """Return the codec `data` is written in, or None when that is unknowable.

    Assumes `data` is the whole file. Resolution order, all deterministic and
    all offline:

    1. a BOM (`utf-8-sig`, `utf-32`, `utf-16`);
    2. a PEP 263 coding cookie in the first two lines, normalised through
       `codecs.lookup` so `latin-1` and `iso-8859-1` are the same answer;
    3. strict UTF-8, which is the only assumption that is safe to make about a
       file with no declaration.

    Returns None - never a guess - when a declared codec is unknown, when a
    declared codec cannot decode the bytes, or when the bytes are not valid
    UTF-8 and nothing declared them. The reason this refuses rather than falls
    back to `errors="replace"` is that the replace fallback is not lossy in a
    harmless way: it rewrites every non-ASCII byte on the way back out.
    """
    for bom, name in _BOM_ENCODINGS:
        if data.startswith(bom):
            return name
    header = b"\n".join(data.split(b"\n", 2)[:2])
    declared = _CODING_COOKIE.search(header)
    if declared is not None:
        try:
            codec = codecs.lookup(declared.group(1).decode("ascii", "replace")).name
        except (LookupError, UnicodeDecodeError):
            return None
        try:
            data.decode(codec, errors="strict")
        except (UnicodeDecodeError, LookupError):
            return None
        return codec
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    return "utf-8"


def detect_newline(data: bytes) -> Tuple[str, bool]:
    """Return the file's dominant newline sequence and whether it is mixed.

    Assumes `data` is the whole file. The second element is True when the file
    mixes conventions, which the receipt reports so a reader is never told a
    CRLF file was written back as CRLF when only most of it was.

    The dominant convention is reported for the receipt and for normalising a
    caller's `old_string`; it is NOT what preserves the untouched content.
    `apply_text_edit` splices raw bytes, so a mixed file stays mixed in exactly
    the places the edit did not touch.
    """
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    if not (crlf or lf or cr):
        # No newline at all: nothing to detect, and the tie-break below would
        # otherwise pick CRLF out of a 0-0-0 tie. "\n" is Python's own default
        # and is the only convention a caller could have written.
        return "\n", False
    mixed = sum(1 for count in (crlf, lf, cr) if count) > 1
    if crlf and not lf and not cr:
        return "\r\n", False
    if lf and not crlf and not cr:
        return "\n", False
    if cr and not crlf and not lf:
        return "\r", False
    if crlf >= max(lf, cr):
        return "\r\n", mixed
    if lf >= cr:
        return "\n", mixed
    return "\r", mixed


def _translate_newlines(text: str, newline: str) -> str:
    """Rewrite every newline convention in `text` to `newline`.

    Used only to let a caller whose `old_string` carries `\\n` find a target in
    a CRLF file. The match is then made against the file's real bytes, so this
    cannot introduce a convention the file did not already have.
    """
    if not newline or newline == "\n":
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline)


def _find_all(data: bytes, needle: bytes) -> List[int]:
    """Every non-overlapping occurrence offset of `needle` in `data`."""
    out: List[int] = []
    start = 0
    while needle:
        found = data.find(needle, start)
        if found < 0:
            break
        out.append(found)
        start = found + max(1, len(needle))
    return out


def _candidates(
    data: bytes, offsets: Sequence[int], encoding: str, context_bytes: int
) -> Tuple[EditCandidate, ...]:
    """Describe each match offset for an `ambiguous_match` refusal.

    Assumes `offsets` is already capped by the caller. Never raises: a preview
    that will not decode is shown with replacement characters, because a
    refusal the model cannot read is a refusal it will retry blindly.
    """
    out: List[EditCandidate] = []
    for offset in offsets:
        low = max(0, offset - context_bytes)
        high = min(len(data), offset + context_bytes)
        preview = data[low:high].decode(encoding, errors="replace")
        out.append(
            EditCandidate(
                offset=offset, line=data.count(b"\n", 0, offset) + 1, preview=preview
            )
        )
    return tuple(out)


def _atomic_write_bytes(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    """Write `data` to `path` atomically, preserving `path`'s mode.

    Delegates to `execution.workspace._atomic_write_bytes` - the primitive the
    whole repository already uses for every durable write - rather than
    re-implementing tmp+fsync+replace here. That function creates the temporary
    in the destination directory, fsyncs the data and the directory, and
    `os.replace`s, so a crash at any instant leaves the ORIGINAL file
    byte-identical and at worst a stray `.tmp` that its own `finally` removes.

    The private name is the cost of not owning `execution/workspace.py`; the
    request to promote it to a public helper is filed in `harness/AGENTS.md`
    under "Cross-terminal requests". The fallback is for a tree where that
    module has not landed: it is the SAME algorithm, not a second one, and a
    test asserts the real primitive is the one in use whenever it is importable.
    """
    from execution.workspace import _atomic_write_bytes as _write

    _write(path, data, mode)


def _resolve_editable(root: str, relative_path: str) -> Tuple[str, Optional[Path], str]:
    """Normalize and safety-check an edit target under `root`.

    Assumes `root` is a directory. Returns (normalized-relative, absolute-path
    or None, refusal-message). The rules are the same ones `syntax_check` and
    `restore_group` already apply: backslashes accepted, traversal collapsed,
    absolute paths and symlinks refused.
    """
    raw = str(relative_path or "").replace("\\", "/")
    if not raw.strip():
        return "", None, "no path was given"
    normalized = _normalize_rel(raw)
    if not normalized:
        return "", None, f"unsafe path refused: {relative_path}"
    if Path(normalized).is_absolute() or ".." in Path(normalized).parts:
        return "", None, f"unsafe path refused: {relative_path}"
    try:
        full = Path(root, normalized)
        if full.is_symlink():
            return (
                normalized,
                None,
                f"symbolic link is not an editable file: {normalized}",
            )
        resolved = full.resolve(strict=False)
        resolved.relative_to(Path(root).resolve())
    except (OSError, ValueError, RuntimeError):
        return normalized, None, f"unsafe path refused: {relative_path}"
    return normalized, full, ""


class EditSession:
    """The read ledger that `require_edit_digest` is enforced against.

    One instance per run/session, owned by the caller; there is no module-level
    state, because a process-global ledger would let one task's read vouch for
    another task's edit. Assumes nothing about its inputs - every method takes
    a repo-relative POSIX path and either the bytes the caller observed or the
    root to read them from.

    The mechanism is the kernel's binding behaviour, not a second one: a digest
    this session recorded is a REAL earlier observation, and it is re-baselined
    after each mutation this session applies, so a bound digest is never a hash
    of the post-call state.
    """

    def __init__(self) -> None:
        self._reads: Dict[str, str] = {}
        self._current: Dict[str, str] = {}

    @staticmethod
    def _key(relative_path: str) -> str:
        """Normalize a path to the ledger's key form."""
        return _normalize_rel(str(relative_path or "").replace("\\", "/"))

    def note_read(
        self,
        relative_path: str,
        data: Optional[bytes] = None,
        *,
        root: Optional[str] = None,
    ) -> str:
        """Record that this session observed `relative_path`, and return its digest.

        Assumes the caller really read the content. Pass `data` when the caller
        already has the bytes; otherwise pass `root` and the file is read here.
        Returns "" when the path is unusable or unreadable, which callers must
        treat as "no observation recorded" rather than as a digest.
        """
        key = self._key(relative_path)
        if not key:
            return ""
        if data is None:
            if not root:
                return ""
            try:
                data = Path(root, key).read_bytes()
            except OSError:
                return ""
        digest = hashlib.sha256(data).hexdigest()
        self._reads[key] = digest
        self._current[key] = digest
        return digest

    def note_mutation(
        self,
        relative_path: str,
        data: Optional[bytes] = None,
        *,
        root: Optional[str] = None,
    ) -> str:
        """Re-baseline `relative_path` after an edit this session applied.

        Assumes the mutation already happened. Marks the path as read (the
        session demonstrably knows its post-image) so a follow-up edit in the
        same session is not refused as "never read".
        """
        return self.note_read(relative_path, data, root=root)

    def was_read(self, relative_path: str) -> bool:
        """Whether this session recorded an observation of `relative_path`."""
        return self._key(relative_path) in self._reads

    def revision(self, relative_path: str) -> Optional[str]:
        """The digest this session last recorded, or None if it never saw it."""
        return self._current.get(self._key(relative_path))


def _config_value(config: Optional[Mapping[str, Any]], key: str, fallback: Any) -> Any:
    """Read one key from a caller's config, falling back to harness DEFAULTS.

    Assumes `config` may be None or an incomplete mapping. Going through
    `harness.config.DEFAULTS` is what keeps this module free of a second set of
    defaults: a knob added there is the knob every task and every eval arm
    gets, which is exactly the property the knob is supposed to have.
    """
    from harness.config import DEFAULTS

    if config is not None and key in config:
        return config[key]
    if key in DEFAULTS:
        return DEFAULTS[key]
    return fallback


def apply_text_edit(
    root: str,
    relative_path: str,
    old_string: str,
    new_string: str,
    *,
    session: Optional[EditSession] = None,
    config: Optional[Mapping[str, Any]] = None,
    validate: Optional[Callable[..., Tuple[bool, str]]] = None,
    trace: Optional[Any] = None,
) -> EditOutcome:
    """Apply ONE exact-block edit to a repository file, or refuse it.

    Assumes `root` is a repository directory the caller owns for the duration of
    the call, `relative_path` is repo-relative, and `old_string`/`new_string`
    are the exact text to replace.     `session` is the read ledger
    (`EditSession`); when omitted a throwaway empty one is used, so
    `require_edit_digest` refuses every mutation until a caller records a real
    read. The strict reading is this primitive's own floor: an ABSENT key, a
    `None` value, or an explicit `True` all mean "on", and only an explicit
    `False` relaxes it (and the receipt then says so). `validate`, when
    supplied, is called as `validate(root, [relative_path])`; the default is
    `syntax_check(root, [relative_path], encoding=<detected>)`, i.e. the check
    reads the file the way the edit wrote it.

    Four properties, each of which a caller cannot get by accident:

    - **Ambiguity is a refusal.** Zero matches is `no_match`; more than one is
      `ambiguous_match` WITH the candidates (line numbers and context), and the
      file is untouched. There is no "replace the first" and no replace-all
      escape hatch, because a coin flip between two call sites is how an edit
      lands in the wrong function.
    - **Bytes are preserved.** The replacement is a splice on the RAW bytes
      (offset found with `bytes.find`), so untouched content keeps its original
      encoding, BOM and line endings exactly. `newlines="mixed"` files stay
      mixed where the edit did not touch them. The file's encoding must be
      determinable (`detect_encoding`); a file whose encoding cannot be
      determined is REFUSED, never rewritten under a guess.
    - **A failed post-edit check rolls back.** The pre-image bytes are captured
      before the write, so a failed `validate` restores the file byte-for-byte
      and the outcome carries `rolled_back=True`. Leaving a known-broken edit
      in the tree for the model to find is a trap: the next turn reads the
      harness's own damage.
    - **Writes are atomic.** Every write and every rollback goes through the
      repository's existing atomic-write primitive, so a crash mid-write leaves
      the original file intact rather than a truncated one.

    Never raises for a refusal - every rejection is an `EditOutcome` with a
    stable `error_kind`, and every one of them is written to `trace` (when one
    is supplied) so a refusal is never silently swallowed.
    """
    normalized, full, refusal = _resolve_editable(root, relative_path)
    candidates_cap = int(_config_value(config, "edit_ambiguity_candidates", 5) or 5)
    context_bytes = int(
        _config_value(config, "edit_candidate_context_bytes", 120) or 120
    )
    post_check = bool(_config_value(config, "edit_post_check", True))
    # Strict is the FLOOR, not a default value: absent, None and True all mean
    # "on", and only an explicit False relaxes it. A caller therefore cannot
    # reach the unsafe behaviour by forgetting a config.
    digest_required = _config_value(config, "require_edit_digest", None) is not False
    max_bytes = int(_config_value(config, "max_file_bytes", 0) or 0)

    def _done(outcome: EditOutcome) -> EditOutcome:
        _log(trace, outcome.trace_kind or "edit_result", outcome.to_dict())
        if outcome.ok and outcome.path:
            # AGT-09: the pre-image is captured here, before the first write
            # this turn touches, and the post-image after the write settles -
            # including after a ROLLBACK, so a rolled-back edit is recorded as
            # the no-op it actually is rather than leaving a stale pre-image.
            _stage_note([outcome.path], done=True)
        return outcome

    if refusal:
        return _done(
            EditOutcome(
                False,
                normalized or str(relative_path or ""),
                ERROR_EDIT_REFUSED,
                refusal,
                digest_required=digest_required,
                trace_kind="edit_refused",
            )
        )

    assert full is not None  # narrowed by _resolve_editable
    if not old_string:
        return _done(
            EditOutcome(
                False,
                normalized,
                ERROR_EDIT_REFUSED,
                "old_string must not be empty (use write to create a file).",
                digest_required=digest_required,
                trace_kind="edit_refused",
            )
        )
    if old_string == new_string:
        return _done(
            EditOutcome(
                False,
                normalized,
                ERROR_EDIT_REFUSED,
                "old_string and new_string are identical; nothing to do.",
                digest_required=digest_required,
                trace_kind="edit_refused",
            )
        )
    try:
        if not full.is_file():
            return _done(
                EditOutcome(
                    False,
                    normalized,
                    ERROR_EDIT_REFUSED,
                    f"file does not exist: {normalized} (use write to create it).",
                    digest_required=digest_required,
                    trace_kind="edit_refused",
                )
            )
        before = full.read_bytes()
    except OSError as exc:
        return _done(
            EditOutcome(
                False,
                normalized,
                ERROR_EDIT_REFUSED,
                f"unreadable: {exc}",
                digest_required=digest_required,
                trace_kind="edit_refused",
            )
        )
    if max_bytes and len(before) > max_bytes:
        return _done(
            EditOutcome(
                False,
                normalized,
                ERROR_EDIT_REFUSED,
                f"file is {len(before)} bytes, over the {max_bytes}-byte edit ceiling.",
                digest_required=digest_required,
                trace_kind="edit_refused",
            )
        )
    pre_sha256 = hashlib.sha256(before).hexdigest()
    # AGT-09: capture the pre-image BEFORE any write, so a refusal later in
    # this call still leaves a usable (if unchanging) snapshot behind.
    _stage_note([normalized], done=False)

    encoding = detect_encoding(before)
    if encoding is None:
        return _done(
            EditOutcome(
                False,
                normalized,
                ERROR_UNDETERMINED_ENCODING,
                f"{normalized} is not valid UTF-8 and declares no usable coding "
                "cookie, so its encoding cannot be determined. Refusing rather "
                "than guessing: re-read it, state the encoding, or convert the "
                "file first.",
                pre_sha256=pre_sha256,
                digest_required=digest_required,
                trace_kind="edit_refused",
            )
        )
    newline, mixed = detect_newline(before)
    try:
        old_verbatim = str(old_string).encode(encoding, errors="strict")
        new_verbatim = str(new_string).encode(encoding, errors="strict")
    except (UnicodeEncodeError, LookupError) as exc:
        return _done(
            EditOutcome(
                False,
                normalized,
                ERROR_EDIT_REFUSED,
                f"edit text is not representable in this file's encoding "
                f"({encoding}): {exc}",
                encoding=encoding,
                newline=newline,
                newline_mixed=mixed,
                pre_sha256=pre_sha256,
                digest_required=digest_required,
                trace_kind="edit_refused",
            )
        )

    # A caller that supplies `\\n` in old_string against a CRLF file must not
    # be told "not found" when the text is plainly there. Try the verbatim
    # form first and only then the file's own convention, so a genuinely
    # ambiguous target stays ambiguous rather than being resolved by the order
    # of the attempts.
    adapted_old = _translate_newlines(
        old_verbatim.decode(encoding, errors="strict"), newline
    )
    adapted_new = _translate_newlines(
        new_verbatim.decode(encoding, errors="strict"), newline
    )
    attempts: List[Tuple[bytes, bytes, bool]] = [(old_verbatim, new_verbatim, False)]
    if adapted_old.encode(encoding) != old_verbatim:
        attempts.append(
            (adapted_old.encode(encoding), adapted_new.encode(encoding), True)
        )

    chosen: Optional[Tuple[bytes, bytes, bool, List[int]]] = None
    for old_bytes, new_bytes, adapted in attempts:
        offsets = _find_all(before, old_bytes)
        if offsets:
            chosen = (old_bytes, new_bytes, adapted, offsets)
            break
    if chosen is None:
        return _done(
            EditOutcome(
                False,
                normalized,
                ERROR_NO_MATCH,
                f"old_string was not found verbatim in {normalized}. Re-read the "
                "file and copy the exact text (indentation and line endings "
                "included).",
                encoding=encoding,
                newline=newline,
                newline_mixed=mixed,
                pre_sha256=pre_sha256,
                digest_required=digest_required,
                trace_kind="edit_refused",
            )
        )
    old_bytes, new_bytes, adapted, offsets = chosen

    # The digest gate fires BEFORE any write, and only for a mutation of a file
    # this session has no observation of. A caller that wants the strict
    # reading relaxed says so in the config, and the receipt says so too.
    ledger = session if session is not None else EditSession()
    digest_relaxed = False
    digest_reason = ""
    if digest_required and not ledger.was_read(normalized):
        return _done(
            EditOutcome(
                False,
                normalized,
                ERROR_STALE_READ,
                f"{normalized} was never read in this session, and "
                "require_edit_digest is on. Read the file first (its digest is "
                "recorded with the read), then retry.",
                encoding=encoding,
                newline=newline,
                newline_mixed=mixed,
                match_count=len(offsets),
                pre_sha256=pre_sha256,
                digest_required=True,
                trace_kind="edit_refused",
            )
        )
    if not digest_required:
        digest_relaxed = True
        digest_reason = "require_edit_digest disabled by config"
    elif session is None:
        digest_relaxed = True
        digest_reason = "no EditSession supplied; nothing could be checked"

    if len(offsets) != 1:
        listed = _candidates(before, offsets[:candidates_cap], encoding, context_bytes)
        more = len(offsets) - len(listed)
        detail = ", ".join(f"line {item.line}" for item in listed)
        if more > 0:
            detail += f", and {more} more"
        return _done(
            EditOutcome(
                False,
                normalized,
                ERROR_AMBIGUOUS_MATCH,
                f"old_string matches {len(offsets)} places in {normalized} "
                f"({detail}). The edit was NOT applied. Extend old_string until "
                "it is unique, then retry.",
                encoding=encoding,
                newline=newline,
                newline_mixed=mixed,
                match_count=len(offsets),
                candidates=listed,
                pre_sha256=pre_sha256,
                digest_required=digest_required,
                digest_relaxed=digest_relaxed,
                digest_relaxed_reason=digest_reason,
                trace_kind="edit_refused",
            )
        )

    offset = offsets[0]
    after = before[:offset] + new_bytes + before[offset + len(old_bytes) :]

    # AGT-03: the check runs on the CANDIDATE CONTENT, in memory, before the
    # write is committed. The post-image bytes already exist at this point
    # (they are a splice of the pre-image), so there is nothing to roll back —
    # a failing edit is DISCARDED, never applied and then undone. `rolled_back`
    # stays True on that refusal because its documented meaning is "the file on
    # disk is byte-identical to its pre-edit state", and `pre_commit=True` says
    # which mechanism got there.
    if post_check:
        from harness.lint import CHECK_FAILED, CHECK_PASSED, render_check

        try:
            candidate_src = after.decode(encoding, errors="replace")
        except (UnicodeDecodeError, LookupError):  # pragma: no cover - encoding
            candidate_src = after.decode("utf-8", errors="replace")
        check = precommit_check(candidate_src, normalized, config=config)
        if check.status == CHECK_FAILED:
            note = render_check(check)
            return _done(
                EditOutcome(
                    False,
                    normalized,
                    ERROR_POST_CHECK_FAILED,
                    f"the edit breaks {normalized}: {check.message}. Nothing was "
                    "written: the new content was rejected BEFORE the write, so "
                    "the file on disk is byte-identical to its pre-edit bytes "
                    "(rolled back: no change was ever committed). Fix the edit "
                    "and retry." + (f"\n{note}" if note else ""),
                    encoding=encoding,
                    newline=newline,
                    newline_mixed=mixed,
                    newline_adapted=adapted,
                    match_count=1,
                    rolled_back=True,
                    digest_required=digest_required,
                    digest_relaxed=digest_relaxed,
                    digest_relaxed_reason=digest_reason,
                    write_mode="pre_commit_refused",
                    pre_sha256=pre_sha256,
                    post_sha256=pre_sha256,
                    pre_commit=True,
                    check_status=check.status,
                    check_line=check.line,
                    check_context=check.context,
                    check_reason=check.reason,
                    trace_kind="edit_rolled_back",
                )
            )
        # An uncheckable file is reported as UNCHECKED, in the tool result the
        # model reads. Applying the edit and saying nothing is the one behaviour
        # this must not have: a caller cannot tell "checked and clean" from
        # "never looked at" if both are silent.
        _note = "" if check.status == CHECK_PASSED else render_check(check)
    else:
        check = None
        _note = ""

    mode = None
    try:
        mode = full.stat().st_mode & 0o7777
    except OSError:
        mode = None
    try:
        _atomic_write_bytes(full, after, mode)
    except OSError as exc:
        return _done(
            EditOutcome(
                False,
                normalized,
                ERROR_EDIT_REFUSED,
                f"atomic write failed: {exc}",
                encoding=encoding,
                newline=newline,
                newline_mixed=mixed,
                match_count=1,
                pre_sha256=pre_sha256,
                digest_required=digest_required,
                digest_relaxed=digest_relaxed,
                digest_relaxed_reason=digest_reason,
                write_mode="atomic_replace",
                trace_kind="edit_refused",
            )
        )

    post_sha256 = hashlib.sha256(after).hexdigest()
    if session is not None:
        session.note_mutation(normalized, after)

    # The on-disk re-read of the same bytes is no longer needed: `check` above
    # examined exactly the bytes this write made visible, before they became
    # visible. A caller-supplied `validate` is a DIFFERENT thing - it is the
    # caller's own gate with a `(root, [paths])` signature that reads the
    # tree, so it keeps its post-write position and its rollback.
    if post_check and validate is not None:
        try:
            ok, message = validate(root, [normalized])
        except Exception as exc:  # a crashing gate is a failed gate
            ok, message = False, f"post-edit check raised: {exc}"
        if not ok:
            restored = True
            try:
                _atomic_write_bytes(full, before, mode)
            except OSError as exc:  # pragma: no cover - disk-level failure
                restored = False
                message = f"{message} (and the rollback FAILED: {exc})"
            if restored:
                if session is not None:
                    session.note_mutation(normalized, before)
                return _done(
                    EditOutcome(
                        False,
                        normalized,
                        ERROR_POST_CHECK_FAILED,
                        f"the edit broke {normalized}: {message}. The file was "
                        "rolled back to its pre-edit bytes and is unchanged; fix "
                        "the edit and retry.",
                        encoding=encoding,
                        newline=newline,
                        newline_mixed=mixed,
                        newline_adapted=adapted,
                        match_count=1,
                        rolled_back=True,
                        digest_required=digest_required,
                        digest_relaxed=digest_relaxed,
                        digest_relaxed_reason=digest_reason,
                        write_mode="atomic_replace+rollback",
                        pre_sha256=pre_sha256,
                        post_sha256=pre_sha256,
                        trace_kind="edit_rolled_back",
                    )
                )
            return _done(
                EditOutcome(
                    False,
                    normalized,
                    ERROR_POST_CHECK_FAILED,
                    message,
                    encoding=encoding,
                    newline=newline,
                    newline_mixed=mixed,
                    newline_adapted=adapted,
                    match_count=1,
                    rolled_back=False,
                    digest_required=digest_required,
                    digest_relaxed=digest_relaxed,
                    digest_relaxed_reason=digest_reason,
                    write_mode="atomic_replace",
                    pre_sha256=pre_sha256,
                    post_sha256=post_sha256,
                    trace_kind="edit_refused",
                )
            )

    return _done(
        EditOutcome(
            True,
            normalized,
            message=(
                f"replaced 1 block in {normalized}" + (f" {_note}" if _note else "")
            ),
            encoding=encoding,
            newline=newline,
            newline_mixed=mixed,
            newline_adapted=adapted,
            match_count=1,
            replacements=1,
            digest_required=digest_required,
            digest_relaxed=digest_relaxed,
            digest_relaxed_reason=digest_reason,
            write_mode="atomic_replace",
            pre_sha256=pre_sha256,
            post_sha256=post_sha256,
            pre_commit=check is not None,
            check_status=check.status if check is not None else "",
            check_line=check.line if check is not None else 0,
            check_context=check.context if check is not None else "",
            check_reason=check.reason if check is not None else "",
            trace_kind="edit_applied",
        )
    )


# --- AGT-09: staged-undo capture around the mutating primitives ---------------
#
# Undo that is a checkpoint STACK is not scriptable and not idempotent, and it
# cannot express "rewind the code, keep the conversation". The mechanism for
# that is `memory.checkpoints.StagedSnapshotStore`; this section is the SEAM
# that connects it to the one place in the harness that knows a file is about
# to change.
#
# The seam is a `ContextVar` rather than a new keyword argument on six
# primitives, because those signatures are other owners' contracts (AGENT-04
# filed the requests to thread a config sink through them and they have not
# landed). With no capture in scope every hook below is one `ContextVar.get()`
# returning `None`, so the default path is unchanged.
#
# The capture discipline is deliberately asymmetric:
#
#   BEFORE a path, ONCE per turn   -> that IS the turn's pre-image for it
#   AFTER  every mutation          -> the post-image is what makes the run's own
#                                    edits ACCOUNTED, so a revert can tell the
#                                    difference between "undo my change" and
#                                    "someone else changed this file"
#
# Nothing here can fail a mutation. Every method swallows its own exceptions
# and records a warning: bookkeeping must never be why a user's work is lost.

_UNDO_DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024
_UNDO_DEFAULT_MAX_SNAPSHOTS = 512
_UNDO_DEFAULT_MAX_TURNS = 64

_ACTIVE_STAGE_CAPTURE: ContextVar[Optional["StageCapture"]] = ContextVar(
    "neo_stage_capture", default=None
)


def _stage_config_value(
    config: Optional[Mapping[str, Any]], key: str, fallback: Any
) -> Any:
    """Read a staged-undo knob from ``Task.config`` with a bounded fallback.

    Key-PRESENCE semantics, the R2-03/R2-06/R2-07 rule: an absent key means
    "no opinion" and takes the module default. None of these keys is published
    in ``harness/config.py::DEFAULTS``, because a default there is merged into
    every task and every eval arm and would silently switch all of them.
    """
    if isinstance(config, Mapping) and key in config:
        value = config[key]
        if value is None:
            return fallback
        return value
    return fallback


def _stage_int(value: Any, fallback: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return fallback


class StageCapture:
    """Best-effort staged pre-images around one run's mutations.

    Constructed by whoever owns the run loop (or by the editor primitives'
    caller), scoped with :func:`stage_capture_scope`, and driven by the
    mutation path itself. Every public method returns a plain dict and NEVER
    raises: a capture failure is recorded in :attr:`warnings` and the step
    continues, because losing the user's work to a bookkeeping failure is the
    one outcome this feature must not produce.
    """

    def __init__(
        self,
        repo_path: str,
        log_root: str,
        *,
        session_id: str = "",
        config: Optional[Mapping[str, Any]] = None,
        trace: Any = None,
        turn_id: str = "",
    ) -> None:
        self.repo_path = str(repo_path)
        self.log_root = str(log_root)
        self.session_id = str(session_id or "")
        self.config = config if isinstance(config, Mapping) else {}
        self.trace = trace
        self.turn_id = str(turn_id or "turn-1")
        self.warnings: List[Dict[str, Any]] = []
        self.captures: List[Dict[str, Any]] = []
        self._store: Any = None
        self._store_failed = False
        self._seen: Dict[str, set] = {}
        self._mutated: Dict[str, List[str]] = {}
        self._counter = 0
        self.enabled = bool(
            _stage_config_value(self.config, "undo_staged_enabled", True)
        )

    # -- store ---------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether a live store could be built; the reason is in ``warnings``."""
        return self.store is not None

    @property
    def store(self) -> Any:
        """The lazily built ``StagedSnapshotStore``, or ``None``.

        Imported lazily so ``harness.editor`` stays importable with no memory
        layer at all, which is the same discipline the rest of this module
        uses for its optional surfaces.
        """
        if not self.enabled or self._store_failed:
            return None
        if self._store is not None:
            return self._store
        try:
            from memory.checkpoints import StagedSnapshotStore
        except Exception as exc:  # pragma: no cover - import guard
            self._store_failed = True
            self._warn("staged_undo_unavailable", f"{type(exc).__name__}: {exc}")
            return None
        try:
            self._store = StagedSnapshotStore(
                self.repo_path,
                self.log_root,
                self.session_id,
                max_file_bytes=_stage_int(
                    _stage_config_value(
                        self.config,
                        "undo_staged_max_file_bytes",
                        _UNDO_DEFAULT_MAX_FILE_BYTES,
                    ),
                    _UNDO_DEFAULT_MAX_FILE_BYTES,
                ),
                max_snapshots=_stage_int(
                    _stage_config_value(
                        self.config,
                        "undo_staged_max_snapshots",
                        _UNDO_DEFAULT_MAX_SNAPSHOTS,
                    ),
                    _UNDO_DEFAULT_MAX_SNAPSHOTS,
                ),
                max_turns=_stage_int(
                    _stage_config_value(
                        self.config, "undo_staged_max_turns", _UNDO_DEFAULT_MAX_TURNS
                    ),
                    _UNDO_DEFAULT_MAX_TURNS,
                ),
                trace=self._emit,
            )
        except Exception as exc:
            self._store_failed = True
            self._warn("staged_undo_unavailable", f"{type(exc).__name__}: {exc}")
            return None
        return self._store

    def _warn(self, reason: str, detail: str = "") -> Dict[str, Any]:
        record = {"reason": str(reason), "detail": str(detail), "turn_id": self.turn_id}
        self.warnings.append(record)
        self._emit("undo_capture_warning", dict(record))
        return record

    def _emit(self, kind: str, data: Mapping[str, Any]) -> None:
        """Send a best-effort event to a caller-supplied trace sink."""
        sink = self.trace
        if sink is None:
            return
        try:
            if callable(sink):
                sink(kind, dict(data))
            elif hasattr(sink, "event"):
                sink.event(kind, dict(data))
        except Exception:
            return

    # -- turns ---------------------------------------------------------

    def begin_turn(
        self, turn_id: str = "", *, paths: Optional[Sequence[str]] = None
    ) -> Dict[str, Any]:
        """Open a turn: capture the pre-image of the whole tree, or of ``paths``."""
        self.turn_id = str(turn_id or self._next_turn())
        return self._capture(
            event="step_before",
            paths=paths,
            label=f"{self.turn_id}:before",
        )

    def end_turn(self, turn_id: str = "", *, clean: bool = False) -> Dict[str, Any]:
        """Close a turn, capturing the post-image of everything it touched.

        ``clean=True`` is the "after clean completion" capture the brief asks
        for: it is the state a successful step leaves behind, recorded as its
        own event so a receipt can say which snapshot it came from.
        """
        target = str(turn_id or self.turn_id)
        self.turn_id = target
        touched = sorted(self._mutated.get(target, set()))
        record = self._capture(
            event="clean_completion" if clean else "step_after",
            paths=touched or None,
            label=f"{target}:{'clean' if clean else 'after'}",
            changed_paths=touched,
        )
        if clean:
            self._record_assistant_message(target, touched)
        return record

    def capture_completion(self, turn_id: str = "") -> Dict[str, Any]:
        """Record the run's clean-completion state for a turn."""
        return self.end_turn(turn_id or self.turn_id, clean=True)

    def _next_turn(self) -> str:
        self._counter += 1
        return f"turn-{self._counter}"

    # -- mutation path -------------------------------------------------

    def observe(self, paths: Sequence[str], *, done: bool = False) -> Dict[str, Any]:
        """Record a mutation about to happen, or one that just finished.

        ``done=False`` captures the pre-image of each path this turn has not
        seen yet. ``done=True`` captures the post-image of every path passed,
        which is what records the run's own content as ACCOUNTED.
        """
        turn = self.turn_id
        normalized = sorted(
            {
                str(item).replace("\\", "/").strip()
                for item in paths
                if str(item).strip()
            }
        )
        if not normalized:
            return {"status": "skipped", "reason": "no paths"}
        seen = self._seen.setdefault(turn, set())
        if done:
            self._mutated.setdefault(turn, []).extend(normalized)
            return self._capture(
                event="step_after",
                paths=normalized,
                label=f"{turn}:mutated",
                changed_paths=normalized,
            )
        fresh = [item for item in normalized if item not in seen]
        seen.update(normalized)
        if not fresh:
            return {"status": "already_captured", "paths": normalized}
        return self._capture(event="step_before", paths=fresh, label=f"{turn}:before")

    def note_mutation(self, relative_path: str) -> None:
        """Record that a path changed, without capturing anything."""
        turn = self.turn_id
        bucket = self._mutated.setdefault(turn, [])
        value = str(relative_path or "").replace("\\", "/")
        if value and value not in bucket:
            bucket.append(value)

    def changed_paths(self, turn_id: str = "") -> List[str]:
        """Return the paths a turn changed, MEASURED by diffing its snapshots.

        The tracked list is an optimization; this is the authority, because it
        compares the recorded pre-image and post-image content rather than
        trusting that every mutation reported in.
        """
        store = self.store
        target = str(turn_id or self.turn_id)
        if store is None:
            return sorted(set(self._mutated.get(target, [])))
        try:
            pre: Dict[str, Any] = {}
            post: Dict[str, Any] = {}
            for row in store.turns():
                if str(row.get("turn_id") or "") != target:
                    continue
                for snapshot_id in row.get("snapshots") or []:
                    record = store.load_snapshot(str(snapshot_id))
                    files = record.get("files") or {}
                    if str(record.get("event") or "") == "step_before":
                        pre.update(files)
                    else:
                        post.update(files)
            changed = sorted(
                path
                for path in set(pre) | set(post)
                if (pre.get(path) or {}).get("hash")
                != (post.get(path) or {}).get("hash")
            )
            if changed or not self._mutated.get(target):
                return changed
        except Exception as exc:
            self._warn("changed_paths_unavailable", f"{type(exc).__name__}: {exc}")
        return sorted(set(self._mutated.get(target, [])))

    def _record_assistant_message(
        self, turn_id: str, changed_paths: Sequence[str], summary: str = ""
    ) -> Dict[str, Any]:
        """Record the turn's changed paths ON the assistant-message row.

        This is the record the undo surface reads back, so a user can see what
        a turn touched without diffing the repository themselves.
        """
        store = self.store
        record = {
            "kind": "assistant_message",
            "turn_id": str(turn_id),
            "changed_paths": self.changed_paths(turn_id) or sorted(changed_paths),
            "summary": str(summary or "")[:280],
        }
        if store is None:
            return record
        try:
            return store._append(record)
        except Exception as exc:  # pragma: no cover - defensive
            self._warn("assistant_message_unavailable", f"{type(exc).__name__}: {exc}")
            return record

    def note_assistant_message(
        self, turn_id: str = "", *, summary: str = ""
    ) -> Dict[str, Any]:
        """Public entry point for recording a turn's changed paths."""
        target = str(turn_id or self.turn_id)
        self.turn_id = target
        return self._record_assistant_message(
            target, self._mutated.get(target, []), summary
        )

    def _capture(
        self,
        *,
        event: str,
        paths: Optional[Sequence[str]],
        label: str = "",
        changed_paths: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        store = self.store
        if store is None:
            return {"status": "unavailable", "reason": "no staged-undo store"}
        try:
            record = store.capture(
                turn_id=self.turn_id,
                event=event,
                label=label,
                paths=paths,
                conversation=bool(
                    _stage_config_value(self.config, "undo_staged_conversation", True)
                ),
                changed_paths=changed_paths,
                scope_prefixes=_stage_config_value(
                    self.config, "undo_staged_scope_prefixes", None
                ),
            )
        except Exception as exc:
            self._warn("capture_failed", f"{type(exc).__name__}: {exc}")
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        if str(record.get("status") or "") == "failed":
            self._warn("capture_failed", str(record.get("error") or "unknown"))
        self.captures.append(
            {
                "turn_id": self.turn_id,
                "event": str(record.get("event") or event),
                "status": str(record.get("status") or ""),
                "files": len(record.get("files") or {}),
                "excluded": len(record.get("excluded") or []),
            }
        )
        return dict(record)

    # -- reporting -----------------------------------------------------

    def report(self) -> Dict[str, Any]:
        """A receipt of what this capture session managed to record."""
        store = self.store
        return {
            "enabled": bool(self.enabled),
            "available": store is not None,
            "session_id": self.session_id,
            "turns": sorted(self._seen),
            "captures": list(self.captures),
            "changed_paths": {
                turn: sorted(set(paths)) for turn, paths in self._mutated.items()
            },
            "warnings": list(self.warnings),
        }


@contextmanager
def stage_capture_scope(
    capture: Optional[StageCapture],
) -> Iterator[Optional[StageCapture]]:
    """Make ``capture`` the active one for the mutating primitives in scope.

    A ``ContextVar`` rather than a module global, so two runs in two threads
    cannot see each other's capture. ``None`` disables the hooks entirely,
    which is what every existing caller gets.
    """
    token = _ACTIVE_STAGE_CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _ACTIVE_STAGE_CAPTURE.reset(token)


def active_stage_capture() -> Optional[StageCapture]:
    """Return the capture in scope for the current context, or ``None``."""
    return _ACTIVE_STAGE_CAPTURE.get()


def _stage_note(paths: Sequence[str], *, done: bool) -> None:
    """Best-effort staged-undo hook used by every mutating primitive."""
    capture = _ACTIVE_STAGE_CAPTURE.get()
    if capture is None:
        return
    try:
        capture.observe(paths, done=done)
    except Exception:
        return


def note_changed_paths(relative_paths: Sequence[str]) -> None:
    """Record changed paths on the active capture's assistant message."""
    capture = _ACTIVE_STAGE_CAPTURE.get()
    if capture is None:
        return
    try:
        for value in relative_paths:
            capture.note_mutation(value)
    except Exception:
        return
