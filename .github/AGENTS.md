# AGENTS.md - `.github/`: CI workflow surface

Four terminals build in parallel, and **each terminal owns one workflow file**.
Separate files are the convention precisely so nobody has to merge someone else's
YAML; do not consolidate these without coordinating.

| file | owner |
|---|---|
| `harness-ci.yml` | harness |
| `execution-ci.yml` | execution |
| `memory-cli-ci.yml` | memory / cli |
| `eval-matrix.yml`, `nightly-quality.yml`, `release-gate.yml` | eval + release |
| `windows-dockerfree-ci.yml` | shared / evals (T5) |

## `windows-dockerfree-ci.yml` (2026-10-01)

A Windows lane that runs a **Docker-free** subset, because the existing
cross-platform matrices run "the Docker-independent majority" WITHOUT
enumerating it - and a subset that is not enumerated cannot be audited.

Four properties, each added because something specific failed:

1. **The subset is an explicit file list, not a glob.** A glob would silently
   absorb a new file and quietly change what the lane proves. The list is
   resolved into `dockerfree_subset.txt` and every later step reads that file, so
   there is exactly one copy of the list.
2. **A guard re-derives Docker-dependence from `pytest --collect-only`** and
   fails if any listed file has grown a Docker-gated test. Without it the list
   rots into a lie within a few rounds - the classic failure being a green job
   that quietly depends on a daemon it does not have.
3. **`HARNESS_EXEC_SKIP_DOCKER=1` is exported job-wide**, so a test that does
   reach a daemon self-skips WITH a reason instead of passing quietly.
4. **Every Python step uses `shell: python`**, not `run: |` with bash syntax, so
   the same script runs on every runner OS.

### The subset was measured, not chosen

Collected every `tests/test_*.py` and split on whether any collected node is
Docker-gated: **157 files / 6588 tests Docker-free**, **11 files / 428 tests
Docker-dependent**. The 11 excluded files are `test_ceiling08_verification`,
`test_ceiling14_resilience`, `test_cli_errors`, `test_cli_neo`,
`test_orchestration`, `test_prompt_cache_cost`, `test_recovery_steering`,
`test_sandbox`, `test_verification_gate_wiring`, `test_verify_js`,
`test_workspace_security`. The lane itself covers **23 files** - the W1 surface
(`shared/`, `evals/`, `memory/`, `extensions/`, `integrations/`, `agent_sdk/`,
`acp/`) plus the hermetic serve tests. It does **not** claim the whole
Docker-free majority; that is harness/cli/runtime territory.

### The three steps, and the defect each one exists for

* **Import smoke** (`tests/test_import_smoke.py`) runs in SUBPROCESSES with a
  scrubbed credential environment. `harness/tool_errors.py` once defined `__new__`
  on a `NamedTuple` subclass and took `import harness.core` down mid-round -
  every already-imported test passed, because only a fresh interpreter sees an
  import-time break. An in-process import test is worthless for that class. The
  same file pins that `import harness.core` does not drag `harness.agent_loop` /
  `harness.agent_kernel` in behind it, and pins that a deliberately broken module
  makes the smoke fail, so the gate is known to be able to fail.
* **Hermetic serve** - `TestServePolicies` HUNG for **>90 s** here. Its tests
  popped `NEO_MODEL` to force a refusal, but `cli.serve.resolve_model` falls back
  to `cli.neoconfig.merged_settings` and found this checkout's own `.neo`
  (`model: openai/gpt-4o-mini`), so it took the serving branch into
  `agent_sdk/server.py:324 serve_forever()` and blocked on a selector forever.
  `socketserver` never returns, so the test did not fail - it consumed the job's
  whole timeout, which reads as an infrastructure flake. Fixed in the test (not
  in `cli/`, which this terminal does not own) by stating the precondition via a
  `no_configured_model` fixture AND replacing `serve_once` with something that
  RAISES, so a regression fails immediately instead of hanging. Measured after:
  **0.77 s for the class, 7 passed.**
* **Latency** - the 5,000-indexed-session listing budget is the one number that
  has to be observed on the CI OS rather than inferred from Linux. The step
  surfaces the measured p95 as a `::notice::` line, so a run that clears the
  budget on a contended runner still records WHAT it measured.

### Verification actually run

* The workflow YAML parses; **every `shell: python` step was extracted and
  `ast.parse`d locally** (a syntax error in one of those steps otherwise shows up
  only as a red Windows job after merge). All 7 compile; all 23 enumerated files
  exist.
* The lane's 21 file selections run locally on this host: **505 passed, 6
  skipped, 2 failed** - see the known-red below.
* `tests/test_ceiling16_surfaces.py::TestServePolicies` -> 7 passed in 0.77 s.
* `tests/test_ceiling03_sessions.py::test_five_thousand_indexed_sessions_list_
  under_100ms_p95` -> 1 passed in 24.16 s.

### Known red in this lane, and it is NOT this lane's code

* `tests/test_module_reachability.py` - 2 failures. `RECORDED_UNREACHED` still
  lists `extensions.skill_policy`, but `extensions/skill_catalog.py:1284` and
  `extensions/skills.py:1839` now import it. The gate is doing its job: the
  module GAINED a production importer and the reviewed backlog was not updated.
  `extensions/` is another terminal's in-flight work and is untracked, so
  deleting the entry from here would race them and would re-break when they land
  their own change. One-line remedy for whoever owns `extensions/`: remove
  `extensions.skill_policy` from `RECORDED_UNREACHED` in that test file.
* `tests/test_ceiling16_surfaces.py::TestDocsTruthGate` - 2 failures from a docs
  version drift (`site/src/lib/content/releases.ts` says 0.2.1, `pyproject.toml`
  says 0.3.0). Not a Windows-lane concern; that file is deliberately NOT in this
  lane's enumerated subset for the lane's own sake, but it is listed in
  `harness-ci.yml` etc. Report, do not silently drop.

**No Docker lane and no live-provider lane were run for this workflow, and
neither is claimed.** Sandbox execution remains gated by the Linux cells of
`harness-ci.yml` / `execution-ci.yml`.

## `ci-lanes.yml` - the six labelled lanes (T5.W1.3, 2026-10-02)

**The convention above still holds.** `harness-ci.yml`, `execution-ci.yml`,
`memory-cli-ci.yml`, `ci.yml`, `eval-matrix.yml` and
`windows-dockerfree-ci.yml` all still exist and still own their module's test
selection. Nothing was consolidated.

What was added is a **labelled lane index**, because "what blocks a merge, what
needs Docker, and how long each lane may take" was written in no single place.
`evals/ci_lanes.py` is that authority (six lanes, as data) and
`tests/test_ci_lanes.py` fails when the workflows disagree with it.

| file | owner | note |
|---|---|---|
| `ci-lanes.yml` | shared / evals (T5) | `smoke`, `trust-ladder`, `host-only`, `docker` and the labelled `windows` entry |
| `windows-dockerfree-ci.yml` | shared / evals (T5) | **now runs the `cli/`-local modules T4 added**, plus `GATE 1/6` |
| `nightly-quality.yml` | eval + release | **now runs the registry gate first**, then the 8-arm matrix and the full suite |

### The rule every lane obeys

`python -m tests.known_failing_pins` runs before the first test invocation in
**all six** lanes, and its exit code is the job's. Not "the lanes that run the
pins" - every lane. The failure this prevents: a pin is promoted, the
`smoke` lane goes red, the other five stay green, the build looks broken for
no reason, and somebody adds the lane to an allowlist.

The registry also catches a pin failing for the **wrong reason** -
`changed_reason` - which is a regression wearing the pin's name and is the
outcome most dangerous to file as a known gap.

### Which lanes block, and why

`smoke`, `trust-ladder`, `docker` and `windows` block. `host-only` and
`nightly` do **not**.

`host-only` is where the host-dependent timing lives (the SLO pins, the
5,000-session p95). It carries `continue-on-error: true` **deliberately**, and
the job logs a `::notice::` saying so: *a flaky timing test in a blocking lane
trains people to ignore the gate, which destroys the gate's value.* A red
there is information, not an obstruction.

The one declared exception is `trust-ladder`, whose rungs are budget
assertions **by construction**. It is named in `evals/ci_lanes.py` and asserted
by `test_the_trust_ladder_exception_is_declared_and_budget_based`, not
implied.

### Two real defects this lane index found

1. **A live `IndentationError` in `release-gate.yml`.** Three lines in a
   `shell: python` step were indented 11 spaces inside a 10-space block. That
   would have failed only on a release run. `test_every_shell_python_step_in_
   every_workflow_actually_parses` extracts and `ast.parse`s every such step in
   every workflow, because a syntax error in one otherwise shows up only as a
   red Windows job after merge. Fixed.
2. **The `nightly` lane pointed at a job that did not exist**
   (`eval-matrix.yml#nightly-quality`). Corrected to
   `nightly-quality.yml#live-quality`.

### The budgets are ceilings, and raising one is a deliberate edit

`evals/ci_lanes.BUDGETS` is checked against each job's `timeout-minutes`, and
a job may only exceed its budget if the budget was raised **in that dict
first** - so the change shows up in a diff on the authority rather than as a
mysteriously slow lane. `windows` is 45 and `nightly` is 200 because the real
jobs declare 45 and 180 for stated reasons (the enumerated Windows subset; the
live multi-provider matrix over 20 real tasks on several providers).

### The Windows lane's `cli/`-local subset is ENUMERATED

`cli/test_cli_import_smoke.py`, `test_display_contract.py`,
`test_render_path_pin.py`, `test_sanitize_pipeline.py`,
`test_sanitize_shapes.py`. A glob would silently absorb a new file and quietly
change what the lane proves. The step also **fails if any enumerated file does
not exist**, because a lane that names a test file which is not there covers
less than it claims and reports success doing it.
