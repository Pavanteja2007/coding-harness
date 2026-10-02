# Release verdict — Neo 0.2.1

**Verdict: BLOCKED. Do not cut over to the new default agent path yet.**

## The one thing that matters

Neo built a new verified agent kernel to replace a legacy loop, then kept the
legacy loop as a compatibility path. The final gate ran both against the same
10 real bugs in 5 real open-source projects.

The new path scored **0 out of 10**. The old path scored **10 out of 10**.

The new path never even got to try. Its own security policy refuses to let the
agent **read the test file that defines whether the bug is fixed** — the file
is inside a protected `tests/` pattern, and the refusal is applied to reading as
well as writing. So the agent sets out to fix a failing test, is told it may not
look at the test, and stops after one turn. Ten times out of ten.

This is a two-line fix (apply the path protection to writes, not reads) and it
is the whole gap. But until it lands, "the default is the strongest path" is
false, and shipping it would be a downgrade.

## What is genuinely working

- **The verifier holds.** Only a real, clean, non-flaky Docker test run mints
  success. Nothing else can.
- **Unverified work is never dressed as success.** It has its own status, its
  own rendering, and a non-zero exit code on every checking surface.
- **Your repository is never modified.** Across all 40 verification runs, a
  content hash of the reference checkout was byte-identical before and after.
- **The daily-driver checklist is 11/11.** Long sessions resume, mid-run
  steering arrives, stale edits are refused, malformed model output cannot wedge
  a session, a dead provider is retried and a dead credential is not, injected
  instructions in an issue cannot exfiltrate anything, detached runs reattach
  with no gaps, sessions search across repositories, read-only review changes
  nothing, headless and the TUI agree, and a 5,000-message session stays flat.
- **No prompt regression:** 14/14 clean.

## The other three blockers

1. **Turning off the knowledge layer crashes the run** (`knowledge_enabled=false`
   is a documented setting; it raises `AttributeError`). The memo flag is a
   boolean and the guard checks for "not None".
2. **A work copy inside your repository is mistaken for your repository.** The
   safe-workspace layer walks up and adopts the enclosing project as its
   identity — it then baselines and protects your *whole* project. Measured cost
   on a large repo: **63 minutes before the agent does anything**. On a toy
   repo: 0.3 s, which is why no test ever caught it.
3. **No clean release candidate.** The shared working tree is dirty, so there is
   no candidate SHA, no reproducible build, and no clean-room install. Committing
   and tagging need your explicit approval; this gate performed no git write, no
   upload, and no publish.

## What was not measured, and is not claimed

- **No live model was available** (the provider's only offered model has no
  channel). Every model call in this gate was a deterministic script. So this
  report says nothing about how good Neo's fixes are — only about the paths.
- The full test suite, the reproducible build, and the clean-room install were
  **skipped**, and are reported as skipped. A skipped lane is not a pass.
- The bugs tested were single-site defects constructed in real upstream source,
  not historical bug reports. The oracles are real upstream tests; the defects
  are ours.

## If you want the unblock

Fix the read/write scope in the path policy first — that alone takes the default
path from 0/10 to comparable with the legacy path. Then fix the two crashes
above. Then re-run one command:

```
python -m scripts.shadow_gate
```

It exits 0 only when the new path is at least as correct as the old one on the
shipped configuration, with zero blockers and nothing skipped. It is written to
refuse to tell you things are better than the evidence: its own test suite
(39 tests) spends its time trying to trick it into a pass.

Nothing in this report was committed, tagged, uploaded, or published.

---

# G0 — the P0 Foundation verdict (2026-10-01, T5 Wave 2)

**`G0_RED`. Exit code 2.** Reproduce:

```powershell
python -m evals.gates.P0 `
  --tests-dir-log <measured `pytest tests/` log> `
  --module-local-log <measured module-local log>
```

This is the honest gate, and it is red. Nothing below was relaxed to change
that: no test was weakened, no threshold lowered, no pin suppressed, no red
reclassified as a skip. An honest red G0 is worth more than a green one
obtained by relaxing something.

## The gate

| status | rung | what it measured |
|---|---|---|
| `pass` | `eval_prompt_self_check` | `python -m evals.run --check` -> **14/14 ok, verdict CLEAN** |
| `not_implemented` | `trust_ladder_suite` | **`--suite trust-ladder` does not exist.** `evals.run` accepts only `auto`, `combined`, `daily-driver`, `prompt-regression`. Owner T5 / P0 |
| `pass` | `known_failing_pin_registry` | `as_recorded=5`, 0 build failures |
| `fail` | `full_suite_tests_dir` | **15 failed, 7,121 passed, 33 skipped** in 5250.04 s |
| `fail` | `full_suite_module_local` | **2 failed, 406 passed**: the designed-red pin + 1 real docs drift |
| `pass` | `docker_daemon_reachable` | Docker Desktop 28.5.1, linux/amd64, overlayfs, 7.614 GiB |
| `blocked` | `live_provider_lane` | no live provider reachable |
| `pass` | `windows_lane` | lane DEFINED (14,608 bytes); **not observed RUNNING** — see §5 |
| `fail` | `security_blockers` | **the count is 1, not 0** |
| `not_implemented` → `pass` | `provenance_spine` | built this wave; `--strict` exit 0 |

counts: **pass=4, fail=3, blocked=1, not_implemented=1** (plus
`provenance_spine`, which was `not_implemented` before this round built it).

## 1. The full suite, honestly

**`pytest tests/ -q -p no:randomly` -> `15 failed, 7121 passed, 33 skipped,
2 warnings in 5250.04s (1:27:30)`, exit 1.**

The 15 failures, all **real**, none a recorded gap:

| node | what |
|---|---|
| `test_aesthetic_gate.py::test_no_hex_literal_outside_the_token_table` | design-token authority (T4) |
| `test_aesthetic_gate.py::test_the_whole_verdict_is_green` | the aggregate of the above |
| `test_ceiling03_sessions.py::test_five_thousand_indexed_sessions_list_under_100ms_p95` | host-contended latency budget (T5) |
| `test_ceiling16_surfaces.py::TestDocsTruthGate::{test_the_real_tree_passes,test_cli_entry_point_exit_codes}` | docs version drift: `releases.ts` says 0.2.1, `pyproject.toml` says 0.3.0 |
| `test_cli_connectors.py::test_duplicate_plugin_labels_keep_winning_source` | T4 |
| `test_cli_plugins.py::test_slash_unknown_lists_available_customs` | T4 |
| `test_cli_theme.py::{test_no_literal_color_outside_the_token_authority,test_every_literal_color_exemption_is_still_used}` | theme token authority (T4) |
| `test_config_trace_state.py::test_get_config_handles_none` | T1 |
| `test_module_reachability.py::{test_every_production_module_has_a_production_importer,test_no_recorded_exemption_may_be_stale}` | `extensions.skill_policy` gained importers; the backlog is stale (T4) |
| `test_scheduler_integration.py::TestWorkerSafety::test_worker_exception_output_redacts_credentials` | **security-relevant** (T3) |
| `test_skills.py::{test_discovery_never_raises_on_missing_roots,test_scan_no_skills_found_is_explicit}` | T1 |

Module-local lane (`harness execution runtime cli mcp_server memory shared
evals scripts dashboard demo`) -> **2 failed, 406 passed, 0 errors**:
`execution/test_unsandboxed_pins.py::test_RED_BY_DESIGN_...` (the *designed*
red) and `demo/test_product_docs.py::test_command_reference_covers_current_registry`
(a real docs drift: 20+ unregistered slash commands).

**Blocked rows: none in either lane.** Nothing was skipped for want of a
daemon, a provider or a credential.

### The lane was invisible until this round

`pytest tests/` collected **7,169** tests and **never collected the 24
module-local test modules inside production packages** — 426 tests, including
*every known-gap registry* in the tree. `testpaths` now enumerates them;
collection is **7,595**. A gate that cannot see the pins recording gaps is not
a gate.

### Reproducibility caveat — read this before trusting the numbers

**These measurements were taken on a tree that other terminals were editing
concurrently.** At least three other `pytest` processes were observed running
against the same working tree during this window, and `tests/collection` moved
by one test mid-run. A concurrent run in this window reported **30** failures
where this one reported **15**, purely because fixes landed in between.

So: **this G0 is a measurement of a moving tree, not of a commit.** It is not
reproducible, and the honest statement is that a reproducible G0 requires a
clean candidate SHA — which is SG-04, which needs human approval to commit.
Nothing was committed, tagged or pushed this round.

## 2. Docker: it RAN. It is not blocked.

`docker info` -> **daemon reachable**: Docker Desktop 4.48.0, Engine 28.5.1,
linux/amd64, overlayfs, 7.614 GiB, cgroup v2, 63 images. `docker version` shows
containerd 1.7.27, runc 1.2.5.

So the Docker-dependent lanes **ran and are included in the totals above** —
they are not skips. The one image-gated lane
(`test_ceiling_r2_12_polyglot.py::requires_go_image`, the Go toolchain) has no
Go base image in `execution/sandbox.py` and self-skips; that is a **blocked**
capability, recorded by T2 as an image gap, and it is counted in the 33 skips
rather than being quietly reclassified.

Caveat: four terminals shared this daemon. Container residue and image-cache
growth under that load are host-pressure observations, not product results.

## 3. Live provider: BLOCKED. Every model call was a scripted double.

`blocked`, with the exact reason: no live provider is reachable. T3 recorded
`ServiceUnavailableError: No available channel` on **3/3** live completions.

**Therefore: no result in this report is a claim about model quality.** Every
model interaction in the suite, in `--check`, and in this gate is a
deterministic scripted boundary. No credential was inspected, requested or
retained by this round.

## 4. Security blockers: **1, not 0.**

The gate measures this, it does not read a flag — and the first version of the
measurement was **wrong**: `Advisory` has no `status` field, so checking for
one reported 0. Reporting zero from a field that does not exist is the
"render an absent value as 0" anti-pattern (`DOCTRINE.md` §1), and it would
have been a false green on a security rung.

So "is this resolved?" is answered by comparing the version this project
actually pins against each advisory's own `fixed` bound:

| advisory | package | severity | pinned | fixed | verdict |
|---|---|---|---|---|---|
| **VEX-ADV-0001** | **litellm** | **high** | **1.74.9** | **1.74.10** | **ACTIVE BLOCKER** |
| VEX-ADV-0007 | setuptools | high | 84.0.0 | 78.1.1 | resolved |
| VEX-ADV-0002 | jinja2 | high | 3.1.6 | 3.1.6 | resolved |
| VEX-ADV-0003 | pyyaml | high | 6.0.3 | 6.0.2 | resolved |
| VEX-ADV-0006 | cryptography | high | 46.0.5 | 46.0.5 | resolved |
| VEX-ADV-0008/9/10 | lodash, minimist, tar | high/critical | — | — | not applicable: absent from `pyproject`, `site/package-lock.json` and the installed set |

**`litellm==1.74.9` in `pyproject.toml` is one release below its own documented
minimum.** That is a one-line fix and it is the single thing standing between
this gate and a zero count.

Two measurement bugs were found and fixed while building this, both of which
had produced a *lower* count: a line-oriented `pyproject` scan missed
`setuptools==84.0.0` because `[build-system] requires` puts the pin on a line
whose first token is `requires`; and a "holder starts with `The `" rule
discarded "The Upstream Authors", the most common real copyright shape.

## 5. Windows: the lane is DEFINED. It has not been observed running.

`.github/workflows/windows-dockerfree-ci.yml` exists (14,608 bytes), runs
`windows-latest` on Python 3.10 and 3.12, enumerates 23 files explicitly, and
has a guard that re-derives Docker-dependence from collection so the list
cannot rot into a lie.

**It was not observed running in this session, and this report does not claim
it was.** It has never been pushed: SG-04 records that the shared tree is
dirty (`6287cb1`, dirty) and that committing and tagging need explicit human
approval, which this round does not have and does not assume. So Windows
coverage is **`not_implemented` in the observed sense** — declared, unproven.
The host this gate ran on *is* Windows (win32, Python 3.10.11), so the lane's
content is not merely theoretical, but a local run is not a CI run.

## 6. What G0 does NOT establish

1. **`--check` is a host self-check of the 14-task prompt set.** It proves the
   eval harness is internally consistent and that no prompt change regressed a
   scripted arm. It is **not** a claim about model quality, cost or latency.
2. **Every model call in this gate and in the whole suite is a scripted
   double.** Nothing here measures model behaviour.
3. **A green suite would be a statement about this tree on this host, not about
   a released artifact.** No reproducible build, no clean-room install, no tag
   (SG-04).
4. **The Docker numbers come from a daemon four terminals were sharing.**
5. **The provenance gate cannot see borrowed-in-spirit code**, only borrowed
   code, and says so in its own output.
6. **This G0 is a measurement of a moving tree** (§1), so it is not
   reproducible until there is a clean candidate SHA.

## The known-failing pin registry

`tests/known_failing_pins.py`, enforced by `tests/test_known_failing_pins.py`
(18 tests). Deliberately-red pins are now distinguishable from real failures
*mechanically* rather than by whoever reads the log most carefully.

- A known-failing pin that **passes** fails the build with `PROMOTE THIS` —
  the promotion mechanism.
- A known-failing pin that is red **for a different reason** is reported as a
  **REGRESSION**, never as a known gap.
- A pin that **cannot be run** is a build failure, never a skip.
- **No blanket suppression**: an AST pass rejects `xfail`/`skip` on any
  registered pin, and a config pass rejects `addopts --ignore`/`--deselect`/
  marker filters. This repository still has **zero** `xfail` markers.
- Demonstrated end-to-end: a planted entry pointing at a green test made the
  meta-test **fail** with `PROMOTE THIS ... Delete the entry from
  KNOWN_FAILING_PINS`; removing it returned 18 passed.

**Populated from measurement, and the honest count is ONE known-failing pin**,
not the five the brief listed:

- **T2's unsandboxed-bash pin** — `execution/test_unsandboxed_pins.py::
  test_RED_BY_DESIGN_no_production_import_path_reaches_the_local_subprocess_sandbox_stub`.
  Red on exactly one site, `harness/agent_loop.py:797`. Owner T1 / P2.1.
- **T3's structural-predictor pin is NOT red, and the brief had the sense
  inverted.** `runtime/invariants.check_structural_guard` **holds** (9/9) and
  `runtime/test_structural_predictor_guard.py` is **16 passed**; both assert the
  predictor is still **unshipped**. It is registered as a deliberately-GREEN
  `InvertedPin`. Filing a green pin as a known failure is the exact confusion
  the registry exists to prevent.
- **No third or fourth designed-red pin exists.** Every other red is real.

## The provenance spine

`scripts/provenance_report.py`, seeded **empty on purpose**: 742 source files
scanned, **0 declared, 0 undeclared**, exit 0 under `--strict`.

The header format mandates the two fields everyone skips: a **`upstream-commit`
hex SHA** (a branch name is rejected — it cannot be re-derived or diffed
against its source) and **`beats-ours-because`** (rejected if under 15
characters, so "mature" cannot stand in for a reason). `modified: yes|no` is
required even when `no`.

A vendored file is detected by **shape** — an explicit `vendored:` block, a
vendor-shaped directory, or a third-party licence/copyright header.
**Demonstrated:** a planted file carrying an Apache header with no block →
`PROVENANCE_GAPS`, **exit 2**; removed → exit 0. Wired into
`release-gate.yml` as a `provenance` job that fails on any undeclared vendored
file and re-checks the scan covered ≥100 files, so a gate that scanned nothing
cannot pass.

Two of its own bugs, both found by its tests: `iter_candidate_files` excluded
vendor directories, making the vendor-directory detector **unable to fire**; and
the walk filtered directories *after* descending, costing **127 s** (the 345×
case `DOCTRINE.md` §8 already records) — pruning in place brought it to
**1.1 s**.

## What a future session must know

- **`pytest tests/` is no longer the whole suite.** 24 module-local modules now
  collect. If you re-narrow `testpaths`, 426 tests including every gap registry
  go dark again.
- **`harness/test_config.py` is production code, not a suite** (2 production
  importers). It yields 1 no-op test and 1 error. It needs renaming, not
  ignoring — the `--ignore` is a migration aid.
- **`trust-ladder` is not a suite.** If a later phase cites it as a green
  measurement, check `evals/run.py` first.
- **`litellm==1.74.9` is one release below its own minimum.** The G0 security
  count is 1 because of it and nothing else.
- The registry in `tests/known_failing_pins.py` is the **only** place a
  deliberately-red test is recorded. Do not add an `xfail`; add an entry.

Nothing in this report was committed, tagged, uploaded or published.

---

## G0 addendum — final measurement, and a LIVE BLOCKER found after it

### The final full-suite run (whole default collection, quiet tree)

`python -m pytest -q --no-header -rf --tb=no -p no:randomly`

```
23 failed, 7592 passed, 33 skipped, 1 warning in 6790.64s (1:53:10)
```

**7,592 passed** is the whole default collection (**7,595** tests, 7,240 of
them in `tests/`). Unlike the earlier run, **no other terminal was running
pytest during this one**, so this number is far more trustworthy than the
`15 failed` figure above, which was taken while three other suites were
competing for the same tree. The honest summary is: **23 real failures on a
quiet tree, and the count is not reproducible while four terminals share one
working directory.**

The 23 = the 15 from `tests/` + the module-local lane's 2 + 6 more that only
appear in the combined run:

| node | lane |
|---|---|
| `runtime/test_boundary_signature_pins.py::test_every_optional_keyword_forwarding_site_in_runtime_is_recorded` | T3 |
| `tests/test_daily_session_human.py::TestAVisualRegressionBecomesADiff::{test_the_corpus_survives_a_repeat_typing,test_the_same_tree_typed_twice_renders_the_same_text}` | T5 |
| `tests/test_dashboard.py::test_server_is_read_only` — `ConnectionAbortedError` | T5 |
| `tests/test_terminal_05_file_change.py::TestDiagnosticsPanel::{test_announce_is_called_with_its_key,test_the_diagnostics_panel_keeps_its_link_callback}` | T4 |

### LIVE BLOCKER, found by the registry, still open

`execution/sandbox.py:2630-2637` is **syntactically invalid**: `def
_with_phases(...) -> ExecutionResult:` has an empty body and is immediately
followed by `def _trace_task_id(...)`. The file raises

```
IndentationError: expected an indented block after function definition on line 2630
```

and **`execution.sandbox`, `execution.verify` and `execution.workspace` are all
unimportable.** `harness.agent_loop`, `cli.main`, `evals.run` and
`runtime.scheduler` still import. This is T2's file and was being edited while
this gate ran (`mtime 03:13:25`), so it is most likely a partially-written
edit rather than a committed defect — but as of this report the `execution`
package does not import, and every sandbox/verify test is blocked on it.

**What this proves about the registry:** `python -m tests.known_failing_pins`
reported the one registered known-failing pin as

```
[FAIL] missing: execution/test_unsandboxed_pins.py::test_RED_BY_DESIGN_...
counts: as_recorded=4, missing=1
1 build failure(s) across 5 registered pin(s).
```

— exit **1**. It did **not** report the pin as red-for-the-recorded-reason
(its expected state), and it did **not** pass. A pin that cannot be *run* is a
build failure, which is mechanic 4 doing its job on a live defect rather than
on a synthetic one.

### The final G0 verdict

`G0_RED`, exit 2. `pass=3, fail=4, blocked=1, not_implemented=1` at the moment
of the last run, because `known_failing_pin_registry` correctly turned red on
the `sandbox.py` breakage above.

**Nothing was done to make this green.** No test was weakened, no threshold
lowered, no pin suppressed, no skip introduced, and no red reclassified.

### 33 skips — what they actually are

Counted, not waved at. They are **blocked** coverage in three groups: Windows
symlink-creation privilege (a host capability), Docker-daemon / Go-image gating
(the daemon IS up here, so these are image- and platform-gated, not
daemon-gated), and `tests/test_provider_smoke.py`'s three live-provider
`skipif` markers.

**That last group is a doctrine violation and is recorded as one.** A
`skipif` on a live-provider lane is "a blocked lane reported as a skip", which
is the Never-do row in `DOCTRINE.md` §1. The markers carry a `reason=`, so they
are not silent, but in a summary table a skip renders identically to a pass. It
was **not** changed this round — `tests/` is T5's, so it is in scope, but
converting it to a `blocked` lane needs a reporting channel that pytest's own
summary does not provide, and inventing one mid-wave would be a larger change
than the fix warrants. Filed rather than half-done.
