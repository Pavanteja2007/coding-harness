"""Agent-written edge-case tests — the pre-success verification gate
(Improvement Round 2, Tasks A+B; owner: Terminal 1's harness).

The gap this closes: the harness used to gate success on the ONE given
failing test plus a full-suite regression run. A fix can satisfy exactly
the given test while still being wrong for cases the issue clearly
implies (boundary values, error conditions, adjacent inputs). A careful
human engineer, before calling a fix done, pokes at those edges — this
gate has the model write those probes, then makes the fix survive them.

Mechanism (one seam, between final-verify-pass and success minting):

  1. GENERATE (Task A): one model call — render_agent_tests_prompt
     (harness.prompts) carries the issue + the candidate fix's unified
     diff; the reply is a JSON object {"tests": [{filename, content}]}.
  2. SANITIZE: filenames must be bare ``*.py`` names (no path parts, no
     separators, no drive/UNC forms, nothing outside ``[A-Za-z0-9._-]``
     besides the extension); contents are compiled (syntax) before use.
     Content total is capped by config. Anything failing sanitize is
     DROPPED, never fatal.
  3. RUN (Task B — same rigor, no lighter path): surviving tests are
     written into a reserved TRANSIENT dir inside a FRESH copy of the
     pristine tree, then evaluated by the SAME verify() the final gate
     uses, in the same two stages every existing test goes through:
       - baseline: verify(pristine+tests, rerun_for_flake_check=0) —
         exactly like the task baseline. A generated test that PASSES
         on the pre-fix tree probes nothing; it is dropped as invalid
         (with a trace event + the run's evidence saved), and only the
         still-failing set proceeds.
       - post-fix: verify(work+tests, rerun_for_flake_check=<same as
         final gate>) — target run(s) WITH flake-rerun semantics plus
         the full-suite regression, identical rigor to the final gate.
  4. GATE: if any surviving baseline-failing test FAILS post-fix, the
     attempt is poisoned — the fix is incomplete for an issue-implied
     edge. The failing test's output (via the structured-feedback
     convention when present, raw tail otherwise) becomes the next
     attempt's feedback, and the tests are saved for human review.
  5. HYGIENE: the transient trees live OUTSIDE work/ (logs/{task_id}/
     agent_tests/{attempt}/...), so work/ is never dirtied; no cleanup
     can leak into a diff. Surviving tests are ALSO saved to
     logs/{task_id}/agent_tests/saved/ for human review regardless of
     outcome; the gate never modifies state.json's files_touched.

Policy (deliberate, symmetric with the lint gate):
  - A generated-test FAILURE is a verification signal -> poisons the
    attempt (retry with feedback), up to the usual max_retries.
  - A GENERATION problem (model call crash, unparseable reply, zero
    tests surviving sanitize, zero surviving the baseline filter,
    verifier crash mid-gate) SKIPS the gate — a verified fix is never
    overturned over test-WRITING quality. Every skip is trace-logged
    with its reason; nothing is silent.

This module holds the pure helpers (parse/sanitize/write/wipe); the
two-stage verify orchestration lives in core.run_task's
_agent_tests_gate (it needs the attempt-loop's verify/cfg/trace).
"""

import json
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__all__ = [
    "copy_with_tests",
    "list_test_files",
    "parse_agent_tests",
    "sanitize_agent_tests",
    "wipe_agent_tests",
    "write_agent_tests",
]


def parse_agent_tests(reply: str) -> Optional[List[Dict[str, str]]]:
    """Parse the generator's JSON reply ({"tests": [{filename, content}]}).

    Tolerates code fences and surrounding prose like _parse_plan_json.
    Returns [{"filename": str, "content": str}], or None when nothing
    parseable is present (caller treats that as skip-gate, not failure).
    Assumes reply is the raw model output; entries missing a filename or
    content are dropped.
    """
    text = reply or ""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    raw_tests = obj.get("tests") if isinstance(obj, dict) else None
    if not isinstance(raw_tests, list):
        return None
    out: List[Dict[str, str]] = []
    for t in raw_tests:
        if not isinstance(t, dict):
            continue
        name = str(t.get("filename") or "").strip()
        content = str(t.get("content") or "")
        if name and content:
            out.append({"filename": name, "content": content})
    return out or None


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.py$")
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_agent_tests(
    tests: List[Dict[str, str]],
    max_files: int,
    max_chars: int,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Enforce the safety/shape rules on parsed tests.

    Returns (kept, reasons) where every entry in kept is
    {"filename", "content"} and reasons carries one human-readable line
    per DROPPED test. Rules (all drop, never fatal):
    - filename: bare name, charset [A-Za-z0-9._-], must end .py, must
      start alnum, length <= 100, no path separators/drive forms (also
      implied by the charset), not already seen (dupes drop).
    - content: compiles as Python source (compile(), SyntaxError ->
      dropped); the accumulated kept total stays within max_chars.
    - at most max_files kept (excess dropped, in order).
    Assumes tests came from parse_agent_tests (already non-empty) and
    max_files/max_chars come from cfg (never hardcoded here).
    """
    kept: List[Dict[str, str]] = []
    reasons: List[str] = []
    seen = set()
    total = 0
    for t in tests:
        name = t["filename"]
        if len(kept) >= max_files:
            reasons.append(f"dropped {name!r}: over the {max_files}-file cap")
            continue
        if len(name) > 100 or not _SAFE_NAME.match(name) or _UNSAFE_CHARS.search(name):
            reasons.append(f"dropped {name!r}: unsafe/malformed filename")
            continue
        if name in seen:
            reasons.append(f"dropped {name!r}: duplicate filename")
            continue
        try:
            compile(t["content"], name, "exec")
        except SyntaxError as exc:
            reasons.append(f"dropped {name!r}: syntax error: {exc}")
            continue
        if total + len(t["content"]) > max_chars:
            reasons.append(f"dropped {name!r}: over the {max_chars}-char content cap")
            continue
        seen.add(name)
        total += len(t["content"])
        kept.append({"filename": name, "content": t["content"]})
    return kept, reasons


def write_agent_tests(
    base_dir: str, rel_dir: str, tests: List[Dict[str, str]]
) -> List[str]:
    """Write tests into base_dir/rel_dir (creating it); returns the
    repo-relative posix paths written, in order.

    Assumes rel_dir is the configured agent_tests_dir (a repo-relative
    posix path like "tests/_agent_generated" — the harness only ever
    passes the sanitized config value) and tests are sanitize_agent_tests
    survivors. Any OSError -> the partially-written list is returned
    (the caller treats a short write as skip-gate, never a crash).
    """
    written: List[str] = []
    root = Path(base_dir) / Path(*rel_dir.split("/"))
    root.mkdir(parents=True, exist_ok=True)
    for t in tests:
        p = root / t["filename"]
        try:
            p.write_text(t["content"], encoding="utf-8")
            written.append(f"{rel_dir}/{t['filename']}")
        except OSError:
            break
    return written


def wipe_agent_tests(base_dir: str, rel_dir: str) -> None:
    """Remove base_dir/rel_dir entirely (missing dir is a no-op).

    Assumes rel_dir is the configured agent_tests_dir. Never raises
    (a failed wipe of a TRANSIENT tree outside work/ must not kill a
    verified task; the dir's reserved name keeps residue harmless).
    """
    p = Path(base_dir) / Path(*rel_dir.split("/"))
    shutil.rmtree(p, ignore_errors=True)


def copy_with_tests(
    src_dir: str, dst_dir: str, rel_dir: str, tests: List[Dict[str, str]]
) -> Optional[str]:
    """Fresh-copy src_dir to dst_dir, then write tests into it.

    Returns dst_dir on success, None on failure (caller skips the gate).
    Assumes src_dir is a snapshot source (pristine/ or work/), dst_dir a
    nonexistent-or-writable path OUTSIDE work/, rel_dir the configured
    agent_tests_dir, and tests the sanitize survivors. A dst_dir that
    already exists is wiped first (stale attempt residue must never
    leak into this attempt's gate).
    """
    try:
        dst = Path(dst_dir)
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(
            src_dir,
            dst,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", "*.pyc", ".git"
            ),
        )
    except OSError:
        wipe_agent_tests(dst_dir, rel_dir)
        shutil.rmtree(dst_dir, ignore_errors=True)
        return None
    written = write_agent_tests(dst_dir, rel_dir, tests)
    if len(written) != len(tests):
        shutil.rmtree(dst_dir, ignore_errors=True)
        return None
    return dst_dir


def list_test_files(repo_path: str, limit: int = 15) -> str:
    """Short listing of existing test files (import-style reference for
    the generation prompt).

    Assumes repo_path is a readable repo tree; scans a few conventional
    test locations (tests/, test/) plus root-level test_*.py, returning
    up to limit repo-relative posix paths, one per line; "" when none
    found. Never raises (an OSError just truncates the listing).
    """
    root = Path(repo_path)
    found: List[str] = []
    try:
        for base in ("tests", "test"):
            d = root / base
            if d.is_dir():
                for p in sorted(d.rglob("test_*.py")):
                    rel = p.relative_to(root).as_posix()
                    if rel not in found:
                        found.append(rel)
                    if len(found) >= limit:
                        return "\n".join(found)
        for p in sorted(root.glob("test_*.py")):
            rel = p.relative_to(root).as_posix()
            if rel not in found:
                found.append(rel)
            if len(found) >= limit:
                break
    except OSError:
        pass
    return "\n".join(found)
