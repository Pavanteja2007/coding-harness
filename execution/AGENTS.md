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

## Definition of done — verified (2026-09-07)

`logs/dod/run_dod.py` runs the whole module against **python-semver**
(real OSS repo, cloned to logs/dod/semver): pristine baseline PASS →
deliberate `_cmp` inversion breaks target+suite (both detected, not
flaky) → fix written INSIDE the sandbox persists to host → post-verify
target PASS + suite PASS + reruns consistent → order-dependent flaky test
correctly FLAGGED (not silently misreported) → git branch/commit/PR
description produced → rationale paragraph grounded in the trace.
**Result: 15/15 checks pass.**

## What's left / future work for this module

- Warm-image pre-build for benchmark batches (Phase 3) — trivial via
  `ensure_image`.
- Poetry/pdm/conda dependency flows — manual image build for now.
- Tests that legitimately need network (`allow_network=True` in verify)
  is plumbed but unexercised on a real case.
- Consider a per-repo "setup command" config key (e.g. `npm`-style repos)
  when multi-language support ever happens (explicitly out of scope now).
- rationale.py: a model-polished variant layered over the deterministic
  draft (call runtime.call_model with the paragraph) — deferred until the
  harness wants it.
