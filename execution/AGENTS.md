# execution/AGENTS.md — Terminal 2 module summary

## What's built (all verified working)

1. **`sandbox.py`** — `execute_sandboxed(repo_path, command, timeout_s=120,
   *, allow_network=False, env=None, mem_limit="1g", cpu_limit=1.0,
   pids_limit=512) -> ExecutionResult` — Docker isolation, exactly the
   INTERFACES.md Boundary-1 signature plus default-safe keyword-only extras.
   - Fresh `--rm` container per call; repo bind-mounted READ-WRITE at
     `/workspace`; `--network none` by default; `--read-only` rootfs +
     tmpfs `/tmp`; `--cap-drop ALL`; `--pids-limit`; mem/cpu limits;
     non-root user; `--pull=never` (no surprise image pulls at run time).
    - Timeout: kills the container by name; returns exit 124 +
      `timed_out=True` (same convention as the old stub).
    - **Round 3 (concurrency):** container names are
      `hexec-p<owner-pid>-<uuid>` — `docker ps` still matches the plain
      `hexec-` prefix; the PID token powers orphan reaping (see Round 3
      section). `execute_sandboxed` sweeps at most once per 30s per
      process for orphans of hard-killed workers. Image builds are
      serialized across processes via a temp-dir lockfile
      (`_CrossProcLock`) — safe to hammer `ensure_image` from N
      scheduler workers on a cold cache.
   - **Never falls back to running unsandboxed**: if Docker is down it
     raises `SandboxUnavailableError`. If you want the old local-subprocess
     behavior, import `harness._stubs.sandbox` explicitly.
   - Dependencies: per-repo image `harness-exec:<fp>` built lazily on
     first use (needs network ONCE for the build — that's the intended
     "task explicitly needs network" case). Fingerprint = base image tag +
     contents of dep manifests only → **code edits never trigger rebuilds**.
     Honors `requirements.txt` + `pyproject.toml [project] dependencies`
     (via a script baked into the build context; the base image ships
     pytest + tomli on python:3.10-slim). Poetry/pdm/conda repos are NOT
     auto-handled — build an image manually or pass `test_command` with
     your own env (see quirks below).
   - **The repo's own package is never pip-installed.** Installed copies
     would shadow the bind-mounted source and tests would exercise stale
     code. Src-layout repos work when their pytest config sets
     `pythonpath = src` (semver does; if one doesn't, pass an explicit
     `test_command` including `-o pythonpath=src` or PYTHONPATH via env).
   - Debug CLI: `python -m execution.sandbox --repo <path> [--network]
     [--timeout N] "<bash command>"`.

2. **`verify.py`** — `verify(repo_path, target_test=None,
   rerun_for_flake_check=1, test_command=None, verify_timeout_s=300, *,
   allow_network=False) -> VerificationResult`. Stateless evaluator of
   ONE repo state (the caller picks pristine vs edited — see the contract
   note below). Runs the target test `max(1, rerun_for_flake_check)` times,
   then the full suite for regression. All test runs go through
   `execute_sandboxed` (isolated, networkless).

3. **`git_output.py`** — `produce_git_output(work_dir, issue_text,
   changed_files, diff=None, verification_summary=None, rationale=None,
   branch_name=None, pristine_dir=None) -> dict` with keys `branch`,
   `commit_sha`, `commit_message`, `pr_description`. Creates
   `harness/fix-<slug>` branch; when the work dir isn't a repo yet, it
   `git init`s and commits the PRISTINE tree first (via git `--work-tree`
   pointing at pristine_dir — no copying), so the fix commit's diff IS
   the fix. Author identity is per-invocation (`-c user.name=...`), never
   writes global git config; user hooks disabled (`core.hooksPath=`).
   Commit subjects follow the project convention: `[fix] <first sentence>`.

4. **`rationale.py`** — `build_rationale(log_dir, issue_text=None) -> str`:
   one grounded paragraph (what was wrong / what changed / why / verdict)
   from `logs/{task_id}/trace.jsonl` + `state.json`. Deterministic, no
   model call. Parses pytest output for the failing test name and error
   line; pulls files/decisions from state.json; verdict from task_end
   status. Empty trace → "" (caller omits the section).

## Contract notes for Terminal 1 (how to call us correctly)

### Task A (Round 3) — exact git_output / rationale interfaces for run_task wiring

Both are pure host-side functions (no Docker, no model calls, no network;
deterministic given their inputs). Import them directly:
`from execution.git_output import produce_git_output, GitOutputError` and
`from execution.rationale import build_rationale`.

#### `build_rationale(log_dir, issue_text=None) -> str`

- `log_dir: str` — `logs/{task_id}/` (the dir, not a file). Reads exactly
  the two files you already write: `trace.jsonl` (T1 schema: events
  `{ts, kind, data}`; the function keys on `task_start` (→ `data.issue_text`
  fallback), `baseline_verify` (→ `data.raw` for the failing-test name +
  error excerpt), `attempt_start` (count → "N attempt(s)"), `task_end`
  (→ `data.status` ∈ success/failed/error/timeout → verdict sentence))
  and `state.json` (Boundary-4: `files_touched` → "changed ..." up to 5,
  `decisions` → first 3 as the "Reasoning:" clause).
- `issue_text` — optional override; wins over the trace's copy when given.
  Pass `task.issue_text` (you always have it).
- Returns: one paragraph, 3–6 sentences ("The issue was that X was failing
  (AssertionError ...). The fix changed `f1`, `f2`. Reasoning: ...; ....
  The fix was verified: ... (after N attempt(s)).") — or `""` when
  `trace.jsonl` is missing/empty (THEN OMIT the PR section — don't insert
  an empty "## What was wrong").
- Never raises for missing/corrupt files (skips blank/corrupt lines,
  returns "" if nothing usable); safe to call on every task end.

#### `produce_git_output(work_dir, issue_text, changed_files, diff=None,
verification_summary=None, rationale=None, branch_name=None,
pristine_dir=None) -> dict`

Call order for run_task's SUCCESS path (after final verify passes, right
where core.py currently does `editor.unified_diff` + `record_file_touched`
— around core.py:454-460):

```python
rationale = build_rationale(str(paths.log_dir), issue_text=task.issue_text)
git_out = produce_git_output(
    work_dir=str(paths.work),                 # the post-fix tree (VERIFY-owned state)
    issue_text=task.issue_text,
    changed_files=editor.changed_files(str(paths.pristine), str(paths.work)),
    diff=diff,                                # your unified_diff output
    verification_summary="target test passed; full suite passed; not flaky",
    rationale=rationale,                      # "" is fine — section omitted
    pristine_dir=str(paths.pristine),         # IMPORTANT — see below
)
# -> {"branch": "harness/fix-<slug>", "commit_sha": "<40-char sha>",
#     "commit_message": "[fix] <first sentence>\n...", "pr_description": "## Problem\n..."}
```

Argument semantics:
- `work_dir` — MUST be the working copy the verifier just passed. It gets
  `git init`ed (if it isn't a repo — your snapshots drop `.git`) and a NEW
  branch is created; the user's checked-out branch is never committed to.
  It must be safe to write `.git/` into — `logs/{task_id}/work/` is.
- `pristine_dir` — STRONGLY RECOMMENDED: pass `logs/{task_id}/pristine/`.
  On a fresh `git init` this stages the PRISTINE tree as the root commit
  (via `git --work-tree` pointing at pristine_dir — no copying), so the
  fix commit's `git show` diff IS exactly the fix. Omit it and the fix
  becomes the root commit (diff still works, but `git log` loses the
  pre/post story). No-op when work_dir is already a repo (branch + commit
  on top of existing history).
- `changed_files` — repo-RELATIVE paths (`editor.changed_files` output
  as-is); used in the commit body ("What changed:") and PR "## Changes".
  More than 20 are truncated in the message (not an error).
- `diff` — unified diff text; truncated to 20,000 chars in the PR
  description's "## Diff" section.
- `verification_summary` — free-form 1–2 lines; lands verbatim in the
  commit body and the PR "## Verification" section.
- `branch_name` — None (default) ⇒ `harness/fix-<slug-of-issue-sentence>`;
  collisions on re-runs auto-suffix `-1`, `-2`, … (never overwrites).
- Returns the 4-key dict — put it in `TaskResult` (e.g. `result.git_output`
  via a config-gated extra field, or into `log_path`'s dir as
  `git_output.json`). `commit_sha` is the FIX commit (parent = pristine).

Error mode: raises `GitOutputError(RuntimeError)` — git missing from
PATH, a git command exits nonzero (message carries command + stderr
truncated to 1000 chars), or a git op exceeds 300s. It NEVER writes your
global git config (identity is per-invocation `-c user.name=...`), never
runs user hooks (`core.hooksPath=`), and never touches the ORIGINAL repo
(all operations are inside work_dir/pristine_dir). Recommended handling in
run_task: catch it AFTER the success result is otherwise final — the fix
is already verified; git output is presentation, so log the error to the
trace (`trace.log("git_output_error", {...})`) and return success with
`git_output=None` rather than failing a verified fix over presentation.
(Your call — but a verified fix shouldn't die because git hiccuped.)

#### Rationale-trace dependency (contract note)
`build_rationale` reads `baseline_verify` events' `data.raw` — core.py
already logs `raw: base_v.raw_output[-3000:]` (core.py:238-243), which is
exactly what the parser wants (the FAILED line + the `___ testname ___`
separator appear in the last 3000 chars of pytest output). Keep that
shape stable. It also tolerates its absence (falls back to the issue
text), so a schema change degrades gracefully — but tell us if you drop
the `raw` key entirely.

- **Baseline division of labor** (already what core.py does): call
  `verify(pristine_copy, target, rerun_for_flake_check=0)` for the baseline
  and read `.target_test_passed`; call `verify(work_copy, ...)` post-edit.
  `verify()` itself ALWAYS returns `baseline_passed=False` — only you know
  both states; you fill the field (`_with_baseline`). This is deliberate:
  verify() cannot reconstruct pristine state from an edited repo_path, so
  doing it internally would be false precision.
- `rerun_for_flake_check` is the TOTAL target-run count, not extra runs:
  0 and 1 both mean one run; ≥2 enables flake detection. A timed-out run
  counts as a distinct outcome (pass/timeout mix ⇒ flaky).
- `test_command` (if given) is used for BOTH target and suite runs —
  target node ids are APPENDED to it, preserving your flags. This matters
  for repos whose addopts require dev-only plugins (e.g. semver's
  `.pytest.ini` needs pytest-cov): pass
  `test_command="python -m pytest -q -o addopts="` to bypass cleanly.
- Docker must be up; otherwise `SandboxUnavailableError`. There is no
  built-in stub fallback — catch it and switch to `harness._stubs.sandbox`
  yourself if you want degraded mode (core.py may want to add that).
- Do NOT re-export `verify` from `execution/__init__.py` — it shadows the
  submodule of the same name (already documented in INTERFACES.md).
- First sandbox call per repo builds its dep image (up to ~5-10 min with
  network); subsequent calls are instant. Batch jobs should warm images
  first via `execution.sandbox.ensure_image(repo_path)`.

## Docker setup quirks worth knowing (this machine: Win + Docker Desktop)

- Bind mounts require the repo path to be under a Docker-Desktop-shared
  drive. `C:\Users` is shared by default; the harness's `logs/` lives
  there, so pristine/work copies always mount fine. Repos on other drives
  may fail the mount — the error surfaces docker's own message.
- Windows paths are converted to `C:/...` (forward-slash) form for the
  `-v` flag; POSIX hosts pass through unchanged.
- Container user is `1000:1000` on Windows/macOS (Docker Desktop mounts
  are uid-agnostic) and the current uid:gid on Linux hosts. Override:
  `HARNESS_SANDBOX_UID=<uid:gid>`.
- `python:3.10-slim` has no `tomllib` (3.11+ only) — the base image
  installs `tomli` for pyproject parsing during builds.
- **Windows checkouts of repos with git symlinks** (e.g. semver's
  `tests/coerce.py` → `../docs/advanced/coerce.py`) materialize symlinks
  as tiny text files containing the link PATH — pytest then dies with a
  SyntaxError importing them. Fix by copying the target file over them
  (the DoD script does this; it's a clone-time problem, not a sandbox
  problem).
- Git pack files from clones are read-only on Windows; rmtree needs an
  onerror chmod handler (pattern used in logs/dod/run_dod.py `_rmtree`).
- Image cache: `harness-exec:base` (one-time) + `harness-exec:<fp>` per
  repo. `docker images | grep harness-exec` to inspect; delete freely —
  they rebuild lazily.

## Tests (all green as of this writing)

- `tests/test_sandbox.py` — unit (path/fingerprint/Dockerfile/argv) +
  Docker-gated integration (isolation: network-blocked, persistence,
  timeout, OOM kill, no container leaks, image caching).
- `tests/test_verify.py` — unit (autodetect/target-command/format) +
  Docker-gated: green/broken/regression/flaky/reerun-0/explicit-command.
- `tests/test_git_output_rationale.py` — real-git tests (fresh repo with
  pristine first commit; existing repo branches without touching the
  original branch; collision suffixing) + rationale paragraphs from
  synthetic traces.
- Docker tests auto-skip when the daemon is down
  (`HARNESS_EXEC_SKIP_DOCKER=1` force-skips, e.g. CI).
- `harness`'s own test suite (test_stubs_and_deps, test_e2e_run_task)
  passes against the REAL execution module — deps.py auto-resolution picks
  it up, no stub swap needed.
- **Round-2 integration re-verified (2026-09-07):** Task A/B outcome —
  T1's core.py call sites (baseline core.py:166, final core.py:318,
  per-step core.py:472) all pass `test_command` + `verify_timeout_s`; the
  real `verify()` accepts both as positional-or-keyword exactly as called.
  Full T1 suite (56 tests incl. 5 e2e bug fixes) re-run with the real
  sandbox+verify in the loop: **56/56 pass in ~2.6 min** (fixture images
  pre-warmed via `ensure_image`; first run without warming is just slower,
  not different). Live `docker ps` sampling during one e2e test observed 3
  fresh `hexec-*` containers (agent bash + target test + suite run),
  proving the suite genuinely drives Docker — not the stub. Zero leaked
  containers and no image-cache growth afterwards. No harness-side or
  execution-side code changes were needed. FYI Terminal 1: the docstring
  in tests/test_e2e_run_task.py still says "REAL local-subprocess sandbox"
  — stale now that deps.py resolves the Docker sandbox; harmless.

## Round 3 (2026-09-08) — concurrency hardening (Task B, verified under real load)

**Found + fixed, all empirically confirmed (not assumed):**

1. **Orphaned containers on hard-killed workers (REAL BUG, worst find):**
   `runtime/stress.py`'s killer (and any scheduler crash-kill) does
   `proc.kill()` = TerminateProcess on worker processes — the worker's
   `docker run` CLI dies but its container KEEPS RUNNING until the command
   finishes; `--rm` only reaps on exit. Measured pre-fix: 8/8 containers
   stayed "Up" after their owners were killed, burning VM memory/CPU for
   the FULL remaining command duration (up to 120s+ each) while 40-50
   other tasks fight for the same 8GB VM.
   **Fix (sandbox.py):** container names now embed the owner host PID
   (`hexec-p<pid>-<uuid>`); every `execute_sandboxed` call runs a
   rate-limited (≤1 sweep per 30s per process) opportunistic sweep
   (`reap_orphaned_containers()`) that kills hexec-* containers whose
   owner PID is dead — surviving peers clean up after killed ones, no
   orchestrator needed. Measured post-fix: orphans reaped within ~35s
   (REAP_INTERVAL_S + sweep time) vs full command duration pre-fix.
   The PID-alive check uses `GetExitCodeProcess` on Windows (NOT
   OpenProcess-success — a killed process's object stays referenced by
   zombie children and OpenProcess keeps succeeding on it; exit code
   STILL_ACTIVE=259 is the only reliable signal). Old-format names
   (`hexec-<uuid>`, pre-PID) are never reaped — owner unknowable.
   `reap_orphaned_containers(dry_run=True)` is public for debugging.
2. **Cross-process image build race:** the scheduler spawns one worker
   process per task, so the in-process `_image_lock` guarded nothing
   across processes. Measured: 10 concurrent cold-cache workers built
   the SAME image 10× (wasteful but correct — docker serializes tag
   writes; all 10 succeeded, ~7s each since layers cache). **Fix:**
   `_CrossProcLock` — an O_EXCL lockfile (holder PID + 30-min stale
   expiry + dead-PID stealing) in the system temp dir; one process
   builds, peers wait on the image cache (2s polls, bounded by
   BUILD_TIMEOUT_S+60, bail-out when the peer's PID dies so a failed
   builder never costs a 31-minute wait). `harness-exec:base` got the
   same guard.
3. **Daemon-crash recovery verified by accident:** Docker Desktop died
   (and was restarted) mid-probe with ~10 containers running — after
   restart: zero hexec-* residue (--rm containers don't survive daemon
   restarts), all 11 dep images intact, suite green. No harness-side
   handling needed beyond the existing SandboxUnavailableError.

**Validation artifacts:**
- NEW `execution/sandbox_stress.py` (Terminal 2's analog of T3's
  runtime/stress.py; NOT in the pytest suite — spawns 40-50 real
  container-driving processes): N task-shaped child processes, each
  doing 3 real sandboxed commands (bash + target-test + suite pytest —
  the execute_sandboxed usage shape of one run_task), staggered start,
  hard-kills mid-container, requeue respawn of the killed ones
  (scheduler semantics), 2s container-census monitor. Checks:
  non-killed all clean / killed respawn+finish / max simultaneous ≤
  tasks / zero hexec residue / image growth bounded to per-fixture
  fingerprints. Runs: **50@50 w/ 7 kills — ALL PASS** (max 36
  simultaneous); **50@cap30-shape w/ 20 kills — ALL PASS** (max 26
  simultaneous, 20 respawned, zero residue). Reports under
  `logs/sandbox-stress/<ts>/sandbox_stress_report.json`. Fixture repos
  are deliberately buggy, so "clean" = sandbox executed genuinely
  (exit 0 or 1, no timeout, no error), NOT exit 0.
- Latency probe (temp workspace): 10 children × `sleep 120` containers,
  6 hard-killed at t+4s, 4 survivors doing normal sandbox calls →
  **all orphans reaped by t+36s** (pre-fix they'd linger ~116s).
- `tests/test_sandbox.py` grew: PID-name parsing / old-format-never-
  reaped / stale-lock stealing / `_maybe_reap` rate limit / `_CrossProcLock`
  semantics (unit), plus Docker-gated `test_orphaned_container_reaped_by_peer`
  (real child process hard-killed mid-container; the surviving test
  process reaps it) and `test_concurrent_burst_no_leak` (20 simultaneous
  calls: all succeed, no residue, image cache unchanged).
- Full-stack re-verified after the changes: my 71 (sandbox+verify+git/
  rationale), T1's 28 e2e/deps (real run_task through the hardened
  sandbox), T3's 12 scheduler-integration — all green.
- NOTE for T3: your runtime/stress.py uses use_fake_harness (no Docker
  in the loop) — it validates YOUR side fine, but it does not exercise
  my sandbox. For real-Docker concurrency load of the full stack, use
  `execution/sandbox_stress.py` (as above) or run T1's e2e suite at
  parallelism; both passed today at 40-50 scale.

## Definition of done — verified (2026-09-07)

`logs/dod/run_dod.py` runs the whole module against **python-semver**
(real OSS repo, cloned to logs/dod/semver): pristine baseline PASS →
deliberate `_cmp` inversion breaks target+suite (both detected, not
flaky) → fix written INSIDE the sandbox persists to host → post-verify
target PASS + suite PASS + reruns consistent → order-dependent flaky test
correctly FLAGGED (not silently misreported) → git branch/commit/PR
description produced → rationale paragraph grounded in the trace.
**Result: 15/15 checks pass.**

## Round 4 (2026-09-08) — DoD re-confirmation + feature-inventory self-audit

### Task A — DoD on a real OSS repo: CONFIRMED (re-run, not just remembered)

Re-ran `python logs/dod/run_dod.py` fresh today against the python-semver
clone — **15/15 checks pass** (independent of Terminal 1's Round-4 run; the
full pipeline: pristine baseline PASS → deliberate `_cmp` inversion detected
in target AND suite, not flaky → fix written INSIDE the sandbox persists to
host → post-fix target+suite PASS, reruns consistent → order-dependent flaky
test FLAGGED → git branch/commit/PR → grounded rationale paragraph). Re-run
again AFTER the verify.py fix below — still 15/15.

### Task B — self-audit vs spec items 4, 5, 26, 27 (found and fixed one real bug)

**Item 4 (sandboxing) — FULLY BUILT.** Docker isolation with `--network
none` default, read-only rootfs + tmpfs /tmp, `--cap-drop ALL`,
`--security-opt no-new-privileges`, mem/cpu/pids limits, non-root user,
`--pull=never`, fresh `--rm` container per call; per-repo dep images
fingerprinted on manifests only; never falls back to unsandboxed
(`SandboxUnavailableError`); concurrency hardening from Round 3
(PID-named containers, orphan reaping, cross-process build lock) all
re-verified this round by the passing test suites.

**Item 5 (verification beyond "tests pass") — ONE REAL BUG FOUND, FIXED.**
Baseline pre-fix pass: implemented (stateless verify() + harness's
`_with_baseline` division of labor, per INTERFACES.md). Flake detection:
worked for pass/fail mixes (real-repo proven in the DoD), BUT the
documented "a timeout counts as a distinct outcome" was FALSE in code:
`outcomes.append(res.exit_code == 0)` collapsed a timed-out run into
"fail", so pass/timeout and fail/timeout mixes read as consistent
outcomes and were NEVER flagged flaky. Worst variant: a test that passed
once then hung on rerun read as a stable PASS (fully "verified").
**Fix** (execution/verify.py + harness/_stubs/verify.py, kept in parity):
three-valued outcome labels "pass"/"fail"/"timeout" (timeout =
`timed_out` OR exit 124); `flaky = >1 distinct label`;
`target_test_passed` still reflects the LAST run. No signature/schema
change — this makes the code match what INTERFACES.md already
documented. **Regression-tested against real Docker** (not synthetic):
`tests/test_verify.py::test_timeout_fail_mix_flagged_flaky` (run 1
times out at 15s, run 2 fails fast → flaky=True; pre-fix: False) and
`test_pass_timeout_mix_flagged_flaky` (run 1 passes, run 2 hangs →
flaky=True; pre-fix: stable-pass misread). Change Log entry added.

**Item 26 (git-native output) — FULLY BUILT.** branch + commit + PR
description, pristine-first-commit so the fix commit's diff IS the fix,
collision-suffixed branch names, per-invocation identity, hooks
disabled, never touches the user's repo; wired into T1's run_task
success path (git.json + trace event, best-effort by contract); e2e
coverage in tests/test_e2e_run_task.py + the DoD's steps 7.

**Item 27 (regression check) — FULLY BUILT, real-repo proven.** verify()
always runs the full suite after the target (even when the target
failed — the harness needs the suite signal either way); the DoD
exercises it on python-semver's real ~200-test suite at every stage
(broken state: regression_detected; fixed state: no-regression), and
T1's loop gates success on `target AND regression AND not flaky`
(core.py:469). The fix above closes the last gap in its interplay with
flaky detection.

**Post-fix test status (all real Docker where gated):** test_verify.py
19/19 (incl. 2 new), test_sandbox.py + test_git_output_rationale.py
54/54, T1's test_stubs_and_deps + test_e2e_run_task 33/33, DoD 15/15.
(My module's suites now total 73 Docker-gated tests; note
`test_concurrent_burst_no_leak` is timing-sensitive when run back-to-back
after the new timeout-mix tests — a 20-burst can race the previous tests'
container teardown. It passes in isolation and on re-run; assertions are
real, the window is the shared daemon's cleanup lag.)
**ROUND-5 CORRECTION to the note above:** both halves of that old note were
incomplete. The test had TWO independent flake mechanisms in different
assertions: (a) the one-shot `docker ps -a` residue check racing `--rm`
teardown (the note above — confirmed live in Round 5 by a reproduced red:
`hexec-p12176-*` visible at the instant check right after the burst), and
(b) the before/after `docker images` comparison done on RAW lists, which
reorders nondeterministically for images sharing a creation second (T3's
2026-09-09 Change-Log diagnosis; reproduced 12/20 raw-list mismatches vs
0/20 sorted on this machine's 25-image cache, which contains an exact
same-second pair from the Round-4 builds). "Passes in isolation" was true
but irrelevant — the flake was order-dependent by construction. Both fixed
in Round 5 (see below); the note above is kept for the record.

## Round 5 (2026-09-09) — flaky-test fix + closeout (final state)

### Task A — `test_concurrent_burst_no_leak` order-dependent flake: FIXED

Started from T3's filed diagnosis (INTERFACES.md Change Log, 2026-09-09
"Terminal 2 flag"): the before/after `docker images` LIST comparison flips
when dep images share a creation second (built ~13 in rapid succession
during Round-4's real-model runs) because `docker images` orders
same-second images nondeterministically. NOT a container leak.

**What the investigation actually found (did not re-diagnose from scratch,
but did verify + extend):**
- T3's mechanism CONFIRMED EMPIRICALLY before touching code: 20 pairs of
  consecutive `docker images` captures over the unchanged 25-image cache →
  **12/20 raw-list mismatches, 0/20 sorted** (the cache holds an exact
  same-second pair, `harness-exec:6cd3c8c0485d`/`eb774f1b4799`, from the
  Round-4 builds). Fix direction from the flag (compare sets) was correct.
- A fresh full-file run reproduced a red of a SECOND mechanism in the
  same test: the one-shot `docker ps -a` residue assertion caught
  `hexec-p12176-*` still in `--rm` teardown right after the 20-burst —
  this is the container-teardown race my own Round-4 note had (correctly)
  suspected but under-ranked. Both mechanisms were real, in different
  assertions of the same test; "the flake" was two flakes.
- Independent sub-agent audit (tests/ + execution/): NO other order-
  sensitive before/after comparisons of external-tool output exist —
  `execution/sandbox_stress.py` already used the set-based pattern (its
  `_images()` set-diff checks are immune by construction), and all other
  `docker ps`/git/pip comparisons are scalars, membership, emptiness, or
  pre-sorted. Also found: 2 more one-shot `docker ps -a` residue checks
  (test_no_container_left_behind, test_orphaned_container_reaped_by_peer)
  with the same teardown-race exposure — fixed alongside.

**The fix (tests/test_sandbox.py only — no production code, no contract
change; the production module never had the ordering bug):**
- `_image_set()` — captures `docker images` tags as a SORTED list (set
  semantics for comparison); used for before/after cache-growth checks.
- `_assert_no_hexec_residue(name_filter=None, timeout_s=15)` — replaces
  ALL THREE one-shot `docker ps -a` residue assertions: polls every 0.5s
  until the matching containers are gone, so daemon-async `--rm` teardown
  can't false-red a finished test. Default scope is THIS process's
  containers (`hexec-p<pid>-`, the Round-3 PID-name contract) — the
  original global `hexec-` check was itself contention-fragile: parallel
  pytest suites (normal on this 4-terminal machine) share one Docker
  daemon, and a global check sees the other suite's LIVE containers.
  Found live during Round-5 verification when two suite runs I'd started
  in parallel tripped each other's residue assertions. A REAL leak
  still fails loudly (leaked names in the assertion message — leaks
  never clear, only teardown does).
- Image-cache growth bounded by `grew <= {repo's own tag}` (the
  `sandbox_stress.py` pattern) instead of exact set equality — tolerates
  the target repo's dep image being lazily built mid-test while still
  failing on any other growth.
- `test_concurrent_burst_no_leak` rewritten to use the helpers.
- `test_orphaned_container_reaped_by_peer`: the direct-reap assertion
  now tolerates a concurrent peer's opportunistic sweep reaping the
  victim first (that's the production self-healing mechanism WORKING)
  and asserts the invariant directly: after our reap, the victim's name
  is gone from `docker ps -a` within the poll window.

**Regression tests (prove the fix under the trigger conditions, not
"ran clean once"):**
- `test_regression_image_list_ordering_immunity` — 8 back-to-back
  `_image_set()` captures over the unchanged cache, EVERY pair must
  compare equal: this is the ordering condition itself (old raw-list
  comparison: 12/20 mismatch rate; must be 0/8 now). Runs in the warm
  cache regime where same-second images exist.
- `test_regression_burst_back_to_back_with_other_tests` — reproduces the
  exact ORDER-DEPENDENT trigger regime: one predecessor container run,
  then the 20-burst IMMEDIATELY after with no settling gap (historically
  red here, green in isolation), asserting the same no-residue +
  bounded-image-growth properties. Green here = the flake's trigger
  ordering is now deterministic, which "passes in isolation" never
  proved.

**Verification:** fixed test green; full file 39/39; full module suite
(test_sandbox + test_verify + test_git_output_rationale) 75/75; burst +
regression tests green across 4 consecutive back-to-back integration-class
runs (the trigger order) and 2 randomized-order full-suite runs
(pytest-randomly, distinct seeds); PLUS two full integration suites run
CONCURRENTLY against the shared daemon (in-order + randomized), both green
— the regime that false-reded the pre-scoping version of this fix. All
Docker-gated, real daemon.

### Task B — final state

All module surfaces unchanged this round: `sandbox.execute_sandboxed` /
`reap_orphaned_containers` / `ensure_image`, `verify.verify`,
`git_output.produce_git_output`, `rationale.build_rationale`. No
production-code changes were needed for this fix — the flake was test
assertion mechanics (list comparison + one-shot teardown checks), so
INTERFACES.md's Change Log entry documents the test-semantics change only;
no contract-visible behavior changed.

## Round 6 (2026-09-09) — adversarial security testing: every attack tried, every outcome

**Verdict up front: every limit and every isolation property HELD under
deliberate attack — sequentially AND under real concurrent adversarial
load. Zero production-code changes were required. One exposure was
CONFIRMED-BY-DESIGN (bind-mount disk write, see the design finding).
Tooling: NEW `execution/sandbox_adversarial.py` (not in pytest — analog
of sandbox_stress.py; `python -m execution.sandbox_adversarial` for Task
A, `--concurrency N` for Task B). Reports:
`logs/sandbox-adversarial/r6-*/` (r6-seq3 = final Task A, r6-conc8b /
r6-conc16 / r6-conc10x3 = Task B). Plus 9 permanent pytest regressions
in tests/test_sandbox.py::TestSandboxAdversarial (84/84 module suite).

### Task A — attacks and outcomes (24/24 HELD, sequential)

*Escape attempts (all blocked by the existing flag set — no fixes needed):*
- **Host mounts**: parsed /proc/self/mountinfo for any mount reaching a
  host dir besides our own /workspace → only-own-workspace-mounted. HELD.
- **Host FS read**: /etc/shadow + /proc/1/root/etc/shadow → PermissionError
  (non-root); /proc/kcore opens but yields 0 bytes to non-root; `..`
  above /workspace clamps at the container root (POSIX semantics —
  `/workspace/../../../etc/passwd` was the CONTAINER's own file, first
  run's false positive); no Users/Windows/Program Files/pavan names
  listable at / or /workspace/.. . HELD.
- **Docker socket**: no /var/run/docker.sock in-container (never mounted);
  AF_UNIX connect impossible. Daemon-takeover path closed. HELD.
- **su root / chown 0:0 / mount -t proc**: all fail — `--cap-drop ALL`
  leaves no CAP_SETUID/CAP_CHOWN/CAP_SYS_ADMIN (non-root + no caps).
  HELD.
- **unshare --user --map-root-user / nsenter -t 1** (info probes):
  blocked by cap-drop+seccomp — but note even where userns creation
  succeeds elsewhere it is non-escalating by itself; our container can't.
- **PID namespace**: pid 1 is the container's own bash, /proc shows only
  the container's own processes. HELD.
- **Env leak**: baseline-diffed container env (image-shipped vars
  excluded) → env-clean. The host passes NOTHING except HOME=/tmp and
  caller-supplied `env` kwarg. (First run flagged GPG_KEY — that's baked
  into python:3.10-slim, public key, not a leak.)
- **Network default**: example.com:80 / 8.8.8.8:53 / 1.1.1.1:443 all
  unreachable under `--network none`. HELD.

*Resource exhaustion (all limits bit — verified by the ATTACK'S OWN
evidence, not just "harness survived"):*
- **Fork bomb** (`bomb() { bomb | bomb & }; bomb`): collapsed in ~1-10s
  with `fork: Cannot allocate memory` (stderr captured) = EAGAIN at
  --pids-limit 512. NEVER survived to the harness timeout. HELD.
  (Caveat for future probes: collapse can exit 0 — the SHELL survives;
  the assertion that matters is "did not time out".)
- **Memory bomb** (instant 2GB) + **memory leak** (64MB/0.3s to 2GB):
  both OOM-killed, exit 137, in 3-9s (leak slower — hits the cap mid-
  allocation). `--memory 1g --memory-swap 1g` = no swap fallback. HELD.
- **Disk fill /tmp** (1GB dd): ENOSPC at EXACTLY 268435456 bytes = the
  256m tmpfs cap. dd exits 1; size is the assertion. HELD.
- **CPU limit** (--cpus 1.0): 4 parallel spin loops took 3.7-6.2x the
  single-loop time (measured T4/T1 ratio; unlimited 12-core VM would
  give ~1.0). HELD.
- **Infinite spin**: killed by the harness timeout at exactly timeout_s,
  exit 124 + timed_out=True. HELD.
- **Output flood** (100MB stdout): returned bounded (~1MB head+tail with
  omission marker — MAX_OUTPUT_BYTES truncation). Host memory safe. HELD.
- **DESIGN FINDING (accepted, documented): disk fill via /workspace** —
  a container CAN write unquota'd into the RW bind mount (dd 256MB
  reached the HOST disk; Docker has no bind-mount quota primitive on
  this setup). This is the product contract: the mount is RW precisely
  so agent edits persist for host-side pristine/work diffing (T1).
  Exposure = a malicious "fix" could waste host disk up to the host's
  free space; bounded in practice by task timeout_s + 1 container CPU.
  If this ever needs closing: per-task disk quota needs devicemapper/
  volume-copy-on-use or a watchdog (future work, NOT needed for CORE).
  Pinned by test_workspace_disk_write_reaches_host_by_design so any
  accidental behavior change is noticed.

*Cross-container interference (custom 2-container scenarios):*
- **Filesystem**: attacker container `find / -name <victim's random
  marker>` while victim runs → zero hits (separate mounts + separate
  container roots; only /workspace differs and they never overlap). HELD.
- **Network default**: networkless containers have ONLY loopback
  (/proc/net/dev = lo alone) — nothing to reach anyone with. HELD.
- **Network opt-in vs victim**: an allow_network=True attacker scanned
  the full /24 bridge subnet (253 IPs x 5 ports) while a networkless
  victim slept → no other harness container reachable (networkless
  victims attach to NO network; default tasks are invisible). HELD.
  Caveat recorded: two tasks that BOTH opt into network share the
  default bridge and could reach each other — inherent to allow_network,
  document-and-accept (no identity/secret flows between them by default).
- **Victim undisturbed**: the sleeping victim completed normally (exit 0)
  despite concurrent attackers. HELD.

### Task B — concurrent adversarial load (78 hostile runs, 0 findings)

- Width 8 x 2 rounds (16 runs): 16/16 HELD, canaries 4/4 clean.
- Width 16 x 2 rounds (32 runs — 16 SIMULTANEOUS bombs on a 12-core/8GB
  VM): 32/32 HELD, canaries 4/4 clean. All mem-bombs OOM'd independently
  (exit 137 each — per-container cgroup limits, no shared budget).
- Width 10 x 3 rounds (30 runs, fork-bomb 4x under concurrency): 30/30
  HELD, canaries 3/3 clean — fork bombs collapsed in 1.6-9.7s each while
  neighbors ran.
- **Canaries** (normal pytest tasks run mid-storm) finished clean every
  time — adversarial load does not disturb well-behaved tasks.
- After all runs: zero hexec-* residue, zero stray fixture repos, no
  `fill` files, image cache grew only by the one shared fixture tag.
- (First concurrent run's 2 "findings" were probe bugs: the concurrent
  corpus's disk-tmpfs entry lacked the size assertion — dd had ENOSPC'd
  at exactly 256MiB, i.e. the limit HELD — and its overall exit 0 came
  from a trailing echo. Fixed; re-runs green. Same lesson as Task A:
  assert on the ATTACK'S evidence (size/duration/marker), never on a
  shell exit code that trailing commands can mask.)

### Round 6 additions

- `execution/sandbox_adversarial.py` — the adversarial corpus as data
  (python payloads written as FILES into fixture repos — zero nested
  quoting failures; verdicts blocked/killed/contained/info/design with
  per-attack checks). Task A mode + Task B mode with canaries.
- `tests/test_sandbox.py::TestSandboxAdversarial` — 9 fast Docker-gated
  regressions: fork bomb collapse, mem OOM, tmpfs cap size, host dirs
  invisible, shadow/pidns isolation, socket+caps, network+interfaces,
  CONCURRENT bomb mix (all limits hold simultaneously), and the pinned
  RW-mount design contract.
- Module suite after additions: **84/84** (test_sandbox 48 + test_verify
  19 + test_git_output_rationale 17). No production code changed.

## What's left / future work for this module

- Warm-image pre-build for benchmark batches (Phase 3) — trivial via
  `ensure_image` (and the new cross-process lock makes concurrent batch
  warm-ups safe now).
- Poetry/pdm/conda dependency flows — manual image build for now.
- Tests that legitimately need network (`allow_network=True` in verify)
  is plumbed but unexercised on a real case.
- Consider a per-repo "setup command" config key (e.g. `npm`-style repos)
  when multi-language support ever happens (explicitly out of scope now).
- rationale.py: a model-polished variant layered over the deterministic
  draft (call runtime.call_model with the paragraph) — deferred until the
  harness wants it.
- Optional: expose `reap_orphaned_containers` via the MCP server / CLI
  (`harness reap`) for operators — the automatic sweep covers the
  harness's own runs; an external entrypoint would cover orphaned
  containers from OTHER harness hosts sharing a daemon (not our setup).
- (Round 6) If the bind-mount disk exposure ever needs closing: per-task
  disk quota via a copy-on-use volume + size cap, or a host-disk
  watchdog — see the DESIGN FINDING in the Round 6 section.
