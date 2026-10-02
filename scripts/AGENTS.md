# Release and installer verification

## 2026-09-25 packaging blocker closure

- `agent_sdk` is an explicit setuptools package. The reproducible candidate
  wheel contains all 12 SDK modules; the sdist contains the same package, and
  the wheel rebuilt from that sdist is byte-identical to the direct wheel.
- Installed-wheel verification runs outside the checkout under `python -I -B`,
  proves module origin is the private venv, runs a deterministic
  `LocalAgent.query()`, and validates its trace and contiguous replay events.
- The clean-room matrix now runs that SDK smoke for wheel and sdist lanes.
  Both Python 3.10 artifact lanes passed `sdk_smoke` before the matrix reached
  the independent Docker-backed fix.
- Windows plain-pip self-uninstall now uses a detached outside-interpreter
  helper that holds the outer console launcher process handle before the CLI
  returns, then removes the distribution after the launcher exits. The
  exact-candidate installed-wheel suite proves the receipt, outer launcher
  PID, external helper interpreter, removed launchers, and removed metadata.
- The exact-candidate installed-wheel suite passes **5/5**. The uninstall and
  release unit selection passes **39 with 1 platform skip**, and the mandated
  release lane passes **108 with 1 platform skip**.
- Two independent immutable source trees built identical normalized sdists and
  wheels; both passed Twine. Final source manifest:
  `ab485eeca54dd2cfd1f1fa6dbc122cddd7daab20b93a0a372fdd5dbee9a2f274`
  (366 files). Final wheel SHA-256:
  `d49d583892ba926cc9a95289b79fb66be3426727e68365f304f756c877d06541`;
  normalized sdist SHA-256:
  `5a9d92cc16c15369c70fc916a8f01872851e5a52ac97a286cd9544f3ef79da59`.
  Rebuilding the wheel from that sdist reproduces the exact wheel hash. The
  live-tree Docker clean-room continuation is
  currently blocked because the Docker Desktop API returns HTTP 500 before any
  model call, not because packaging, SDK smoke, or uninstall failed.

## 2026-09-25 diagnostic artifact

Current dirty-source 0.2.1 artifacts pass `python -m build` and
`python -m twine check dist/*`. Exact SHA-256 values:
wheel `0c6ceb98794aac6e01b56b8576e6f02624ebe54f07cbec5a840a5b5f2fba40fd`;
sdist `e1b75d215e8db6441f48c5337ece534fb9b3cbb00303ad255f0d40dd8501c8de`.
Both passed fresh external Python 3.10 installs and smoke checks. They are
diagnostic only: the worktree is dirty, lint/full-suite/daily-driver gates are
not green, and no reproducibility or publication claim is made.

## VEX-RELEASE-09 delivery path (2026-09-25)

- `verify_release.py` now provides stable JSON/NDJSON reports and exits,
  sanitized Git provenance, optional clean/tag requirements, ignored generated
  roots, exact `SHA256SUMS`, order-independent `Requires-Python`, sdist
  normalization, wheel/sdist payload checks, and two-build comparison.
- `--sbom` validates a CycloneDX 1.x document, requires every direct project
  dependency to exist in the locked component set, completes the root
  `dependsOn` graph atomically, and records the enriched SBOM hash. The current
  79-component runtime SBOM validates against CycloneDX 1.6; its enriched SHA-256
  is `16d42f6f6b3a87c6a10d2f6505557495965714b7802b94d267e7a01f5949a71d`.
- `clean_room_matrix.py` creates one fresh venv per Python/artifact lane, strips
  credentials and ambient pip configuration, uses lane-first PATH, records exact
  artifact hashes, and checks install/import/version/help, Docker, packaged
  smoke benchmark, a real scripted Docker fix, Git output, login/logout, update,
  and real console-script uninstall. Setup and lane failures remain machine
  readable; blocked is never passed.
- `github_workflow.py` is a read-only fixed-argv `gh` adapter for issue-to-task
  packets and PR-review evidence. It redacts credentials and exposes no comment,
  approval, merge, push, or upload operation.
- `release-gate.yml` runs hostless release/evaluation/TUI regressions on Linux,
  macOS, and Windows across Python 3.10-3.12; required Linux/Docker lanes;
  complete collection; two-build plus sdist-to-wheel verification; exact
  checksums; a strict CycloneDX SBOM; Twine; clean rooms; native installers; and
  a fail-closed aggregate. It never uploads or publishes.
- Installers support `NEO_FORCE_VENV=1`, best-effort update checks through
  `NEO_SKIP_UPDATE_CHECK=1`, strict release checks through
  `NEO_REQUIRE_UPDATE_CHECK=1`, and exact fresh Windows command resolution.

### Current evidence and honest blockers

- Packaging and Windows launcher blockers from the prior round are closed.
- Current exact-candidate evidence: installed-wheel **5 passed**; SDK own suite
  **22 passed**; uninstall/workflow unit selection **39 passed, 1 skipped**;
  mandated release lane **108 passed, 1 skipped**.
- Python 3.10 wheel and sdist clean-room lanes both passed install, pip check,
  package-origin checks, functional `sdk_smoke`, version/help/PATH/login/logout,
  packaged smoke, and Docker-daemon discovery. They then stopped at the real
  fix because Docker Desktop returned HTTP 500 before attempt 1/model call 1.
  This is a blocked environmental continuation, not a packaging failure.
- Reproducibility is proven from identical immutable source trees, independent
  of concurrent edits in the live shared checkout. Publication still requires a
  reviewed clean commit/tag and remote CI; no commit, tag, push, upload, or
  publication was performed.


## Scope

This module owns the release artifact verifier, the clean-room install
matrix, installer/update/uninstall release tests, and the root release
documentation. It does not own the CLI, harness, execution, runtime, or
memory implementation.

## Built

- `scripts/verify_release.py` checks exact wheel/sdist filenames, project
  metadata, entry points, configured package payload, generated-bytecode
  exclusion, timestamps, SHA-256 hashes, and two-build reproducibility.
- `scripts/clean_room_matrix.py` creates a fresh isolated venv for every
  Python/artifact lane, strips credentials from the child environment,
  checks package imports and version/help contracts, runs the packaged
  smoke benchmark, checks Docker, runs a real Docker-backed scripted fix,
  verifies trace evidence, and checks source/package/original fixture
  integrity while ignoring generated Python caches.
- Release documentation records exact-artifact publication and the
  isolated Python 3.10–3.12 matrix.

## Release integration update (2026-09-24, Terminal 4)

- The release verifier caught fixture `__pycache__/*.pyc` entering
  artifacts through implicit package-data discovery. `pyproject.toml` now
  disables implicit data and excludes bytecode; `MANIFEST.in` enforces
  the same rule for sdists. A fresh build then passed
  `verify_release.py` and `twine check` for both exact 0.2.1 artifacts.
- The current disposable `tests/test_installed_user_flow.py` run built one
  wheel and one sdist, validated license/MCP metadata and package payload,
  installed the wheel into a private venv, and passed 2/2. This is a
  development verification, not a publishable clean candidate.
- `.github/workflows/release-gate.yml` now gates the artifact pair,
  private installed flow, prompt matrix, and the Next.js site gates.
  Docker-backed full evaluation also passed locally after Docker Desktop
  became available: 14 tasks × 8 arms, 112/112 clean.
- The canonical `python scripts/lint_ratchet.py` is currently BLOCKED by
  pre-existing/new findings in parallel work (`harness/_stubs/verify.py`,
  `harness/decision_memory.py`, `runtime/ablation_memplan.py`,
  `tests/test_decision_memory_planning.py`, and untracked scratch probes).
  Do not update the baseline to hide these findings.
- The final artifact hashes are recorded in the current-source section
  below; earlier clean-stage reports are historical and must not be used
  for publication.


The current source candidate was rebuilt in the clean stage
`C:\Users\pavan\AppData\Local\Temp\opencode\neo-t6-release-stage-20260924-04`.
The normalized artifacts are:

- wheel: `bcc7569f22ba6473f1c816d8a87b652858ea55390b4ffd4da6bffeea9dc1370b`
- sdist: `d83f5f7d23ba09bbb0bd38d5c707e02d44c2a64e54f3bab2f41b3dd1bca8af8f`

`release-verification-final.json` confirms two-build reproducibility,
`twine check` passed for the exact wheel, normalized sdist, and
SOURCE_DATE_EPOCH-controlled wheel rebuilt from that sdist, and
`sdist-wheel-verification-final.json` records the sdist-to-wheel check.
The final clean-room report is
`logs/product-round/terminal-6/clean-room-matrix-release-final.json`:
Python 3.10.11, 3.11.15, and 3.12.13; wheel and sdist lanes; 96 checks
passed, 0 failed, 0 blocked, 0 skipped. The prior Docker outage report
is retained as `clean-room-matrix-current.json` with blocked, not passed,
Docker-dependent checks. The complete release test set is 105 passed,
1 skipped; the installed-user-flow regression is 2 passed.

## Mocked and real paths

The matrix uses `HARNESS_SCRIPTED_MODEL` only to make the installed CLI
fix deterministic and credential-free. It does not stub installation,
the scheduler, the Docker daemon, the sandbox, or the verifier. Each
real-fix lane records baseline failure, final target/regression success,
and non-flaky trace evidence. The packaged smoke benchmark uses the
shipped fixture and the documented fake-harness configuration.

## Known limitations and handoff

- The current host has no `py` launcher, so Windows launcher probing is
  covered by parser/installer tests rather than a live `py -3` install.
- One-line installers were not run against the real user PATH/registry;
  their isolated regression tests and fresh-child checks are the safe
  substitute.
- PyPI publication, public metadata verification, and revocation of the
  previously exposed token remain human/owner actions. No upload, tag,
  commit, or push is performed here.
- The final build emits only benign manifest warnings that no bytecode
  files were present to exclude; the verifier confirms all three fixture
  data files are present in both artifacts.
- The shared worktree contains concurrent uncommitted changes. Preserve
  them and re-read status/diffs before any future release build.

## VEX-FINAL-13 integration update (2026-09-25)

- `pyproject.toml` now includes `acp`, `agent_sdk`, `extensions`,
  `integrations`, `recipes`, and `recipes.builtin` in the explicit wheel
  package list, and ships the two builtin recipe YAML files.
- `scripts/verify_release.py` treats `evals` as a valid product package root;
  release payload checks still reject generated logs, caches, and forbidden
  output roots.
- Release regressions cover the expanded package inventory and installed
  `recipes.builtin` module.
- Focused release tests pass: `17 passed` for
  `tests/test_release_workflows.py`; combined release and installed-user
  checks pass: `20 passed`.
- A copied source snapshot built twice with `SOURCE_DATE_EPOCH=1790072773`
  passed `scripts/verify_release.py` with `reproducible: true`, normalized
  sdist verification, and `python -m twine check`. The installed-user flow
  against that wheel passed `3 passed` in a private venv.
- Snapshot wheel SHA-256:
  `cae4e6fe8b7ddcd7c2df72a2f3af08e844b2668cb261d960a4c3dcc19ca697ed`.
  Snapshot sdist SHA-256:
  `5ea2600953e7a585202fff8cbf38cbb50c8f10ef0104c21e01be079a5d53a136`.
- A source-tree build pair was rejected because the parallel TUI test process
  changed untracked `acp/client.py` between builds. This is an integration
  blocker, not evidence of packaging nondeterminism; do not publish the
  snapshot hashes.
- Final release remains fail-closed: the checkout is dirty and untagged, the
  repository-wide Ruff check reports 91 findings and format check reports 111
  files needing formatting, the full suite and daily-driver readiness are not
  green, Docker is unavailable, live-provider and LSP/manual-repair evidence is
  incomplete, and the known runtime resume-identity and Windows uninstall
  defects remain cross-owner blockers.

## VEX-FINAL-13 final attempt update (2026-09-25)

- The live tree has 2,582 collected tests, 371 changed paths, and no tag.
  Docker is available (`28.5.1`).
- `python -m pytest -q -p no:randomly` was attempted for one hour and
  reached approximately 61% before the tool timeout; it is not a complete
  suite result.
- The Docker/e2e shard returned **186 passed, 1 skipped, 1 failed**. The
  failure was `tests/test_e2e_run_task.py::test_retry_recovers_after_failed_first_attempt`;
  the test passed in isolation, while the full e2e file failed under
  concurrent Docker load and low free disk space.
- Four targeted failures were observed while their owner files were being
  edited by parallel sessions: `tests/test_agent_loop.py` (legacy SUCCESS
  label pin), `tests/test_cli_auth_release.py` (TUI logout cache refresh),
  `tests/test_cli_tui.py` (unknown-role style pin), and
  `tests/test_model_router.py` (ledger write failure pin). Runtime/TUI
  handoffs explicitly say not to overwrite their in-flight work.
- `python -m evals.run --suite prompt-regression --check --json` is
  **CLEAN, 14/14, zero skips**; `tests/test_release_workflows.py` is
  **17 passed** with scoped Ruff clean.
- Final repository-wide Ruff statistics report **100 findings** and
  `ruff format --check` reports **141 files** needing formatting.
- A source-stability-guarded two-build release pair was rejected because
  `runtime/model_router.py` changed during build B. No mixed artifact is
  publishable; rerun only after the shared tree is frozen.

## VEX-FINAL-13 / R2-19 — the final ceiling gate (2026-09-27)

**Verdict: `BLOCKED`.** Full report with every measured number:
`logs/architecture-round/terminal-13.json`. Read that file, not this section,
for the numbers. What follows is what a next terminal must know without
re-running anything.

### What landed in this module

- **`scripts/reproducibility_guard.py` (NEW, this terminal's own file).**
  The guarded two-build lane. `python -m scripts.reproducibility_guard
  --work-dir <dir> --source-date-epoch <n> [--keep-stages] [--report <path>]`.
  Exit 0 `reproducible`, 1 `not_reproducible`, 2 `blocked`/`invalid`.
- **`tests/test_ceiling_r2_19_gate.py` (NEW).** 22 tests on that guard's own
  honesty, named after behaviour.

Nothing else was edited. `pyproject.toml`, `verify_release.py`,
`test_release_workflows.py` and `test_installed_user_flow.py` were not
touched this round, and **no config key was added to any `DEFAULTS` table**.

### The guard, and why it is not `verify_release.py`

`verify_release.py` compares a dist directory you hand it. It cannot tell you
the two artifacts in it came from the same bytes of source — which is the
failure Round 1 hit twice (`acp/client.py` then `runtime/model_router.py`
changed mid-build, and both pairs were correctly rejected and produced no
evidence). So the guard is a separate lane and the guard IS the product:

1. the live tree is **content-sampled** (never git-status) before staging and
   again after each build; any movement makes the verdict `invalid`, and
   `reproducible` is `false` even when the two artifacts are byte-identical;
2. each build runs in its **own independent read-only stage**, so build B never
   reads a file build A could have touched;
3. each stage is **re-sampled from disk by the guard**, not trusted on the
   copier's own report (that weaker version was defeatable and its own test
   proved it — see the three defects below);
4. the wheel is compared byte for byte; the sdist is compared **after**
   `verify_release.normalize_sdist`, and the **raw** hashes and sizes are
   reported alongside, because on this toolchain the raw sdists differ (22
   bytes measured) and a report showing only the normalized digest would let
   "reproducible" be read as "byte-identical" when it is not.

`STAGE_EXCLUDED_DIRS` excludes `logs`, `dist`, `build`, `graphify-out`,
`Temp`, `site-v1-backup`, `neo_agent_cli.egg-info` and the caches. `logs` is the
important one: the product's own log root lives inside the checkout.

### Measured on this tree

- **Guarded two-build: `reproducible`.** 918 staged files / 229,148,051 B,
  digest `0781c871…`, sampled 3× with zero movement; two read-only stages,
  four digests (2 copier + 2 re-sampled) all equal; two real builds 31.3s and
  31.0s, both exit 0.
  - wheel `neo_agent_cli-0.3.0-py3-none-any.whl`, 2,238,535 B,
    `52522a36c323670052ce1c0aa7566c6f6682476ee40ae7ec93c3788d89de1a33` —
    **byte-identical across both builds**.
  - normalized sdist 2,895,762 B,
    `97721ae027245f42f765d7c6e1d876eb1dbdf5a35c3ea97f56cc831fc71e0a69` —
    identical after normalization; **raw differed (2,927,262 vs 2,927,284 B)**.
  - `python -m scripts.verify_release … --compare-dist … --normalize-sdist` →
    exit 0, `status: pass`, version 0.3.0. `python -m twine check` → both
    PASSED.
- **Full suite** `python -m pytest -q -p no:randomly --tb=line -rf` →
  **4250 passed, 5 failed, 24 skipped in 54m40s**, 4279 collected, on a tree
  verified quiet (no python/pytest/evals process running) and stable.
- **Prompt regression** `--check` 14/14 CLEAN (twice, including at close);
  full matrix **14 tasks × 8 arms = 112/112 success+verified, 98/98 valid
  comparisons, 0 regressions, verdict CLEAN**.
- **Daily-driver** `--suite daily-driver --json` → 26 cases × 2 arms,
  **52/52 arms ok**, `false_verified_successes 0`, `unauthorized_mutations 0`,
  `lost_edits 0`, feature-evidence lane 28/28, Docker canary
  `completed_verified` with a real verification receipt. Readiness
  `NOT_READY` (see G2-05).
- **Adversarial suite as ONE invocation** (R2-15 + R2-03 + R2-12 + R2-14) →
  **266 passed, 1 skipped** (the Go-toolchain test; no base image).
- **This module's tests** → **44 passed** (17 + 5 + 22) in 8m46s.
- **Lint**: `ruff check` clean on every file this module owns, including both
  new ones. `ruff format` applied to the two new files only.
- **Hygiene**: free disk 18.40 → 17.07 GiB (**−1.33 GiB** for the whole gate;
  Round 1 leaked ~25 GB). Docker images 44/11.11 GB → 47/12.12 GB
  (**+3 harness-exec variants, +0.94 GB**). Every file added or changed over the
  gate was this terminal's own two new files plus build residue.

### The seven blockers (all in the JSON with reproduction and next action)

- **G2-01 the site publishes v0.2.1 and the wheel is 0.3.0.**
  `site/src/lib/content/releases.ts:23`; `python -m scripts.docs_truth` exits 2;
  two tests in `tests/test_ceiling16_surfaces.py` red. Deterministic. **One
  line in R2-18's file.** Do NOT fix it by lowering `pyproject`'s version.
- **G2-02 two skills tests are not hermetic.** `tests/test_skills.py:127,275`
  assert "no skills found" from the default roots, but
  `%APPDATA%\neo\plugins\demo\skills\demo\SKILL.md` exists (installed
  2026-09-27 01:33), so they fail on any machine that installed a plugin and
  pass on a clean CI runner. Fix by pinning the roots to an empty temp dir —
  **not** by relaxing or deleting the assertions.
- **G2-03 the lint ratchet is red on three files, none of them mine and none of
  them a Round-2 file**: `head_iv.py` (untracked 80 KB **UTF-16LE** root
  scratch file — ruff cannot parse it; delete it), `measure_batch_phase.py`
  (tracked; 4 auto-fixable), `tests/test_cli_theme.py` (5, created during
  Round 2). The baseline was **not** raised and no `extend-exclude` entry was
  added to hide `head_iv.py` — that is the same act wearing a different hat.
- **G2-04 no live provider is reachable.** Four ambient credentials, four
  distinct measured causes. Tokenrouter *reaches its endpoint and its
  credential is accepted* but the gateway now returns `RateLimitError: "This
  request requires a challenge to be completed."`; bynara says "a valid API key
  is required"; nararouter is a connection error. **No model-quality claim is
  available for this tree.** Only each credential's env-var NAME, byte length
  and prefix class were recorded.
- **G2-05 daily-driver readiness `NOT_READY`.** 16/17 required quality
  capabilities observed (`cost_latency_user_intervention_quality` missing), the
  live-provider lane not selected, and the sampled real manual-repair record
  has 3/3 `UNKNOWN` so the 0.90 threshold is unevaluable — reported `null`, not
  `0.0`. That is the correct behaviour; the evidence is simply missing.
- **G2-06 `self_critique` is an ACTIVE round feature with no ablation arm.**
  The runner says so itself (`feature_coverage.complete: false`,
  `uncovered: ["self_critique"]`). So "any prompt changed in Round 2 must pass
  the ablation" is measured for **6 of 7**. `harness/prompts.py` was modified
  inside the Round-2 window and git cannot separate Round 1 from Round 2 (one
  commit, 378 uncommitted paths).
- **G2-07 not a blocker, recorded flake.**
  `tests/test_ceiling03_sessions.py::test_five_thousand_indexed_sessions_list_under_100ms_p95`
  failed in the full run at p95 103.6 ms against a 100 ms budget and **passed
  standalone**. Load-sensitive timing pin.

### Three defects this round's own new tests found in its own new tool

1. the guard trusted the copier's `StageReport` about itself — a stage whose
   accounting was wrong would have been accepted. Now re-sampled from disk and
   checked against both the report and the live sample;
2. a build that exited 0 without producing artifacts reported the vague
   "did not complete"; now named exactly, because "exited 0" and "produced
   artifacts" are different claims and only the second is evidence;
3. `_compare` raised `KeyError` on a report with no `blockers` key — a latent
   contract bug that would have crashed any caller outside `guarded_two_build`.

### Not implemented — stated, not implied

- The guard is **not** wired into `.github/workflows/release-gate.yml`. It is
  a runnable lane, not a CI step.
- `source_stable` covers the **staged** set (918 files), not the whole working
  tree. A build-residue change outside that set is not caught; the denominator
  is printed so the scope is visible, but the wider claim is not made.
- No SBOM, no tag check, no clean-git check — those are `verify_release.py`'s
  and `release_evidence.py`'s lanes and were deliberately not duplicated.
- Nothing was published, committed, tagged, or pushed. `git` was used
  read-only. `logs/` is gitignored, so all of this is local evidence.

### Cross-terminal requests

See `cross_terminal_requests` in `logs/architecture-round/terminal-13.json` —
six items, each with a file and the smallest next fix. The three that block the
verdict are G2-01 (site version), G2-02 (skills-test hermeticity) and
G2-03 (three lint files). **None of them is this terminal's file, and none was
edited.**
