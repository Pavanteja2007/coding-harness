# execution/AGENTS.md — Terminal 2 module summary

## P0/W2 — the containment and honesty claims, pinned (2026-10-01)

**FOUR NEW `execution/`-local test modules. ZERO production lines changed.**
`execution/sandbox.py`, `verify.py`, `flake_gate.py`, `baseline_set.py`,
`containment_audit.py` and every other production module are **byte-identical**
to their pre-session state (SHA-256 verified after the weakening demonstration
below). Nothing outside `execution/` was edited either. **+2,759 lines of
tests, 0 lines of product.**

| file | lines | what it pins |
|---|---:|---|
| `execution/test_containment_pins.py` | 824 | the 7 containment invariants, at argv level, no daemon |
| `execution/test_flake_gate_pins.py` | 623 | the three-valued flake gate + the `.flaky` read-site scan |
| `execution/test_unsandboxed_pins.py` | 615 | **the inverted pin** + the stub-reachability audit |
| `execution/test_verification_honesty_pins.py` | 697 | the 4 `VerificationResult` defaults + statelessness |

Run them with:
`python -m pytest execution/test_containment_pins.py execution/test_flake_gate_pins.py execution/test_unsandboxed_pins.py execution/test_verification_honesty_pins.py -q`
-> **54 passed, 1 deselected** (the deselected one is the inverted pin; see
§3). With the pin included: **54 passed, 1 failed**, which is the designed
state.

### 1. The seven containment invariants, pinned at argv level

Each row of Wave 1's attack table is now a test that fails when the claim is
weakened. All of them inspect the argv `execute_sandboxed` would actually spawn,
so they run in 1.2 s with no daemon.

| # | claim | the assertion that breaks if weakened |
|---|---|---|
| 1 | VCS metadata read-only inside a writable workspace | every overlay spec ends `:ro`; the workspace mount must NOT end `:ro` (agent edits must persist); the SET of read-only dirs is itself pinned so a shorter list cannot make the pin vacuous |
| 2 | a writable root never overlaps a read-only path | all four hostile declarations absent from the resolved set AND one `overlaps` note each; the resulting argv carries no `:rw` for them |
| 3 | an unusable writable/read-only path is DROPPED with a note | `_normalize_repo_relative` returns `""` for 10 hostile shapes (never a clamp); each refusal leaves a `refused a ... declaration` note; refusals must not flip `workspace_readonly` |
| 4 | an ABSENT read-only path is reported absent, not mounted | it stays in `readonly_subpaths`, is absent from the mount list, lands in `readonly_absent`, and resolving + building the argv does **not create the directory** |
| 5 | network off unless declared | `--network none` present by default; ABSENT (not merely overridden) when a declaration turns it on; the receipt names the declaration |
| 6 | declared writable roots make the workspace `:ro` | BOTH mounts present and correct; mount ORDER is asserted (overlays after the workspace, so Docker's deeper-target rule narrows rather than being shadowed) |
| 7 | a fresh container per call | `--rm` + a `--name` that differs between two constructions; no `-d`/`--detach`; an AST scan proves no other production module can reach a container-REUSING docker verb (`exec`/`start`/`attach`/`restart`/`commit`/`cp`) outside the one declared owner (`warm_sandbox.py`); and `warm_sandbox` refuses `purpose="verification"` **at construction**, with `hostile=True`/`reuse=False` proven to start nothing |

Three properties every pin there depends on, and they are what make it more than
a snapshot of current behaviour:

- **Non-vacuity runs FIRST.** `test_the_gate_is_not_a_rubber_stamp_before_anything_else_is_asserted`
  requires the pre-spawn gate to refuse five distinct mutations before any
  acceptance assertion is trusted. A gate that accepts everything would make
  every "the product's own argv is accepted" assertion meaningless, so this
  fails first.
- **The two halves must agree.** Every argv pin also asserts
  `assert_sandbox_argv_isolated` ACCEPTS the argv `resolve_containment` produced
  for the same policy. A policy that resolves correctly while the gate refuses
  its own argv means the deciding half and the checking half disagree, which is
  a defect in one of them and cannot be told apart from outside.
- **The demonstration is real.** `test_a_read_write_vcs_overlay_is_refused_by_the_pre_spawn_gate`
  takes the argv the product builds, rewrites every `:ro` to `:rw`, and requires
  the gate to refuse it. That is the literal edit a future weakening makes.

### 2. The flake gate, and the `.flaky` read-site scan

Three assertions, all green, plus a scan with **zero unlisted sites**:

1. Fewer than two OBSERVED outcomes -> `not_run`, never `not_flaky`. The
   control is `test_two_or_more_observations_is_the_threshold_and_it_is_derived`,
   which reaches `not_flaky` and `flaky_detected` at two — so `not_run` cannot
   pass because the gate never fires.
2. A **caller** bug degrades to `not_run`. Three shapes: asked 3 / reported 1,
   asked 2 / reported 0, asked 0 / reported 1. Each asserts `not_run` AND that
   the mismatch is recorded in `notes` AND that the observed vector is
   preserved verbatim, so a reviewer can recompute the verdict from the receipt.
3. The AST scan (see §5 for the full enumeration).

Two scan details worth keeping. First, it is an AST pass over THREE syntactic
forms — `obj.flaky`, `getattr(obj, "flaky", None)`, and `obj["flaky"]` — because
a grep misses the `getattr` form, which is how
`execution/baseline_set.py::classify_run` and both `verification_gate.py` readers
actually read the field. Missing that form would leave three real read sites
unpinned, which is the direction this pin has to be right in. Second, a site
passes only if it is (a) in a module that OWNS `flake_check`, (b) paired with a
`flake_check`/`detection_possible` read in the same function, or (c) in
`ALLOWLIST` with a **reason and a direction** (`"refuse"` = a wrong answer can
only produce more refusal; `"gap"` = the read carries the documented
misreading and is recorded as such). Stale allowlist entries fail too, because an
exemption with no subject is a permission.

### 3. The inverted pin — RED BY DESIGN

**Exact name for T5's known-failing registry:**

```
execution/test_unsandboxed_pins.py::test_RED_BY_DESIGN_no_production_import_path_reaches_the_local_subprocess_sandbox_stub
```

It **fails today** on exactly one site, `harness/agent_loop.py:797`
(`from harness._stubs import sandbox`). It goes green when T1 deletes that path.
**Do not xfail it, skip it, delete it, or add `harness/agent_loop.py` to
`NON_LIVE_REACH_SITES`.** A companion test
(`test_the_inverted_pin_is_registered_as_known_failing_and_not_suppressed`)
asserts by AST that no `xfail`/`skip` decorator exists on it, that the name is
in this module's registry, and that `NON_LIVE_REACH_SITES` is still the exact
two-entry tuple — so suppressing the pin fails a test too.

### 4. The stub-reachability audit (green)

Enumerated by AST over every packaged module, three import forms, and **closed**:

| site | why it is not a live hole |
|---|---|
| `harness/agent_loop.py:797` | **LIVE.** The unsandboxed bash path. T1 / P2.1. |
| `harness/deps.py` (4 sites) | each reaches its stub only when BOTH `HARNESS_USE_STUBS`/`HARNESS_ALLOW_STUB_FALLBACK` is set AND the real package is absent. Pinned by AST: the `except ModuleNotFoundError` handler must consult `_stub_fallback_allowed` AND `_boundary_missing`. |
| `harness/_stubs/verify.py` | the stub verifying itself; inside `_stubs/`. |

`SandboxUnavailableError` is the correct behaviour and is pinned three ways:
the raise itself with a sentinel file proving the command did not run; an
argv-level pin that `execution/sandbox.py`'s **only** spawned program is the
`docker` CLI (resolved through local list variables, so the pin is not
theatre); and `execution.sandbox` exposing **no** host-execution entry point at
all. `execution/**` production source reaches zero stubs.

### 5. `.flaky` read sites — the full enumeration

15 sites in `execution/`. `read`/`w` is the AST classification; the `g` sites
are `getattr(...)` reads, which a grep would miss.

```
FILE:LINE    FORM         FUNCTION                      CLASSIFICATION
flake_gate.py:296   attr         FlakeVerdict.flaky       property: flake_check == FLAKE_DETECTED  (OWNER)
flake_gate.py:355   attr.read    FlakeVerdict.to_dict     RECEIPT (same dict carries flake_check)
flake_gate.py:520   attr.read    FlakeRun.flaky           property: delegates to verdict.flaky  (OWNER)
flake_gate.py:922   attr.WRITE   attach_evidence          THE projection; allowlisted + separately pinned
verify.py:834             attr.read    _attach_structured_feedback  REFUSE  (adds feedback only)
baseline_set.py:833        getattr-read _outcome_for               REFUSE  (flaky=True -> "fail")
baseline_set.py:1192       attr.read    blocks_success             **GAP**  (see 8.2)
baseline_set.py:1257       attr.read    BaselineVerdict.to_dict    RECEIPT
baseline_set.py:1388       getattr-read classify_run                REFUSE  (defaults to None, not False)
baseline_set.py:1400       getattr-read classify_run (degraded)     REFUSE  (same)
verification_gate.py:599   getattr-read _baseline_verdict          **GAP**  (see 8.2)
verification_gate.py:629   getattr-read _baseline_snapshot         REFUSE  (pre-fold snapshot)
verification_intelligence.py:189 attr.read  to_dict               RECEIPT
verification_intelligence.py:197 attr.read  to_dict               RECEIPT
verification_intelligence.py:318 attr.read  run_verification      REFUSE  (absent -> True = blocks)
```

### 6. Verification actually run (this tree, `-p no:randomly`, real Docker 28.5.1)

- **Required lane** `test_sandbox.py test_verify.py test_workspace_security.py`
  -> **142 passed** (234.92 s).
- **Required lane** `test_ceiling_r2_02_flake.py test_ceiling08_verification.py`
  -> **119 passed** (103.24 s).
- **Required lane** `test_verify_js.py test_ceiling_r2_12_polyglot.py` ->
  **119 passed, 1 skipped** (76.61 s). The skip is the image-gated Go-toolchain
  lane: BLOCKED coverage, not a pass.
- **NEW pins** -> **54 passed, 1 deselected** (35.24 s); with the inverted pin
  included, **54 passed, 1 failed** = the designed state.
- `python -m evals.run --check` -> **14/14 ok, verdict CLEAN**, exit 0.
- `python -m ruff check execution` -> **All checks passed!**
- `python -m compileall -q execution` -> exit 0.
- `git diff --check -- <my four files>` -> **exit 0**; hand-verified 0
  trailing-whitespace lines and a trailing newline in each. The UNSCOPED
  `git diff --check` exits **2** on trailing whitespace in
  `cli/AGENTS.md`, `harness/AGENTS.md` and `runtime/AGENTS.md` — **other
  terminals' uncommitted markdown, not this round's files.**
- `docker info` -> daemon **reachable**, 28.5.1, 12 CPUs, 7.614 GiB,
  overlayfs, cgroup v2, 66 images. `docker ps -a --filter name=hexec-` ->
  **0 rows** after every lane: no container residue from this round.
- **No live-provider lane was run** and none is claimed; no credential was
  inspected, requested or retained.

### 7. The weakening demonstration (fail -> restore -> pass)

One line in `execution/sandbox.py` was changed, the pins run, the change
reverted, the pins re-run.

**Weakened** — `execution/sandbox.py` `_docker_run_args`, the read-only overlay
loop:

```python
# BEFORE
for source, target in readonly_mounts or ():
    args += ["--volume", f"{_win_to_docker(source)}:{target}:ro"]
# AFTER (the weakening)
for source, target in readonly_mounts or ():
    args += ["--volume", f"{_win_to_docker(source)}:{target}:rw"]
```

**FAIL** (8 of 20 pins red — one per claim that depended on `:ro`):

```
E           AssertionError: assert 'rw' == 'ro'
E            - ro
E            + rw
execution\test_containment_pins.py:631: AssertionError
=========================== short test summary info ===========================
FAILED execution/test_containment_pins.py::test_vcs_metadata_is_mounted_read_only_inside_a_writable_workspace
FAILED execution/test_containment_pins.py::test_a_read_write_vcs_overlay_is_refused_by_the_pre_spawn_gate
FAILED execution/test_containment_pins.py::test_a_writable_root_overlapping_a_readonly_path_is_recorded_and_loses
FAILED execution/test_containment_pins.py::test_an_unusable_writable_root_is_dropped_with_a_note
FAILED execution/test_containment_pins.py::test_an_absent_readonly_path_is_not_mounted_because_docker_would_create_it
FAILED execution/test_containment_pins.py::test_the_network_is_off_unless_a_declaration_turns_it_on
FAILED execution/test_containment_pins.py::test_declaring_writable_roots_makes_the_workspace_mount_read_only
FAILED execution/test_containment_pins.py::test_the_readonly_policy_workspace_and_overlays_stay_compatible
8 failed, 12 passed in 2.00s
```

**RESTORED** -> `20 passed in 1.19s`, and the file is byte-identical to the
pre-weakening copy:

```
current : 87C73D2D58C0E821737AEBC1BDBF91C02788D71528B3ACE34C6BCE413BB73A31
backup  : 87C73D2D58C0E821737AEBC1BDBF91C02788D71528B3ACE34C6BCE413BB73A31
identical: True
```

### 8. Findings this round did not anticipate

**8.1 — `verify()` DOES mutate the repository it evaluates, and my first pin
was wrong about it.** My first draft asserted "no file under `repo_path`
changes". Running it produced a red I did not discount: `verify()` creates
`.pytest_cache/` (4 files) on every run. That is not a defect and not something
to paper over — the container's `/workspace` is a **read-write bind mount by
design** (agent edits must persist for host-side diffing), so a suite CAN write
and pytest DOES. Asserting otherwise would have been a false claim shipped as a
test. The pin was rewritten to the invariant that actually protects the diff:
**no file the repository declares as its own content may change, and anything
that appears must already be on the project's generated-artifact list** — and it
delegates to the repository's OWN
`execution.workspace.is_generated_path` rather than a list written in the test,
so a path the project already treats as generated passes by construction.
A landed `.pytest_cache` is now a recorded fact of the design, not a red.

**8.2 — two `.flaky` reads carry the documented misreading and are recorded
as KNOWN GAPS, not fixed.** Both are fail-closed everywhere except in the
`not_run` case, where `flaky=False` is indistinguishable from a checked-stable
run:

- `execution/baseline_set.py:1192` `BaselineVerdict.blocks_success` reads
  `self.flaky` and never consults `flake_check`, so a `flake_check="not_run"`
  verdict arrives as `flaky=False` and does not block.
- `execution/verification_gate.py:599` `_baseline_verdict` renders the reason
  *"the target test passed, the full suite passed, and the target was not
  flaky"* for a check that never ran. The rung cannot DISAGREE with the harness
  mint (pinned over the whole boolean cube by
  `test_the_baseline_verdict_rung_cannot_disagree_with_the_harness_mint`), and
  `plan_fold` only ever CLEARS, so neither can widen a claim — but the WORDING
  of a receipt asserts stability it did not measure.

Both are on the `ALLOWLIST` with `direction="gap"`, which requires the reason to
contain the literal `KNOWN GAP`, so a reader sees them. Fixing either is
`flake_check` awareness in a fail-closed gate — a behaviour change to a gate that
can only ever refuse, so it is filed rather than applied unilaterally.

**8.3 — `execution/warm_sandbox.py` is the only module that may reach a
container-reusing docker verb, and that is asserted rather than assumed.** Row 7
needed "assert no call site reuses a container", which a grep cannot answer (the
drivers' own prose matches). It is an AST pass over executable string literals.
`warm_sandbox.py` is allowlisted by NAME with the reason (opt-in
`hostile=`/`reuse=`, refuses `purpose="verification"` at construction, identity-
enforced slot), and a new reuse site anywhere else fails.

**8.4 — `harness/deps.py` reaches FOUR stubs, not one.** The brief's audit asked
about the local-subprocess stub; enumerating the whole `harness._stubs` surface
found `model_router`, `verify` and `scripted_model` reachable from the same
resolver. All four are env-gated identically, so the conclusion is unchanged,
but the audit's enumeration is now closed over all of them rather than over the
one the question named.

**8.5 — three false failures in my own first drafts, all caught rather than
worked around.** (a) `_split_volume_spec` returns `""` for the workspace mount,
so asserting "every mount has an explicit mode" failed on the one mount that is
*supposed* to rely on Docker's default. (b) An explicit `readonly_paths` list
REPLACES `DEFAULT_READONLY_SUBPATHS` rather than adding to it — the `None` vs
`[]` distinction the module's own docstring calls out — so my "the default set
survives a bad entry" expectation was wrong and is now split into two arms.
(c) `ast.AST` has no `__eq__`, so matching a load-site `Name` against an
assignment target `Name` by identity never matches; the argv resolver reports
every real `docker` call as UNRESOLVED and would have made the pin worthless.
Each is a test-side fix, not a product change.

**8.6 — the `test_selection.py` filename collision is live.**
`execution/test_selection.py` is a PRODUCTION module whose name matches
pytest's `test_*.py` discovery pattern, and `pyproject.toml` pins
`testpaths = ["tests"]`. So `python -m pytest execution/` would try to collect
it as a test module. My four new files sit beside it, so this round ran every
lane by EXPLICIT path. Not a defect today (the explicit-path lane is what CI
would do anyway) and not fixed here, but a future `testpaths` change would
collect a production module.

### 9. Cross-terminal requests

1. **T5 — register the inverted pin** (see §3 for the exact name). It is the
   only red in the module and it is red ON PURPOSE.
2. **T1 / P2.1 — delete the unsandboxed bash path** in
   `harness/agent_loop.py` (~line 797). Nothing else needs to change anywhere:
   the inverted pin turns green with no other edit, which is what makes the
   deletion provable rather than asserted.
3. **T1 — `harness/agent_kernel/legacy.py` still defaults a MISSING
   `regression_passed` to `True`.** `execution/` is pinned on the strict side of
   that line (`test_a_missing_regression_term_is_never_read_as_passed`), so the
   two halves disagree by design and the divergence should be either closed or
   recorded the way `execution/`'s gaps are.

## P0/W1 — the subprocess-output ingress, and the containment claims attacked (2026-10-01)

**NEW: `execution/ingress.py`, `execution/containment_audit.py`,
`execution/sandbox_cost.py`. EDITED: `sandbox.py`, `workspace.py`,
`verify.py`, `flake.py`, `warm_sandbox.py`, `git_output.py` (docstrings
only), `result_parsing.py` (docstring only), plus the five pre-existing ruff
findings in `env_snapshot.py` / `feedback.py`. `harness/**`, `cli/**`,
`runtime/**`, `shared/**`, `memory/**`, `evals/**`, `tests/**` and
`.github/**` were NOT edited. `shared/security.py` was NOT edited; it was
CALLED. No verifier mint, completion status, exit code, or event kind
changed. `INTERFACES.md` was NOT edited (see request U-6).**

### 0. What was actually true before this round

`shared.security.redact_text` had **ZERO call sites in `execution/`**. The
only redaction in the package was `git_output.py`'s own 50-line local regex
table (`_redact_secrets`, kept — see U-2) and one call on the tool-ARGUMENT
path for an effect hash (`workspace.py:4288`). Every `ExecutionResult.stdout`
reached a model, a journal row and a terminal render **verbatim**. Measured on
this tree before the change: `cat config.py` inside a sandbox returned
`API_KEY = "sk-live-AAAABBBB..."` to the model, in the same call the
`read` tool returned the same key unredacted.

### 1. THE INGRESS — one function, and the invariant

`execution/ingress.py::seal_output` is the single point through which all
subprocess and file-content output passes. The invariant, stated once and
carried in the module as `INGRESS_INVARIANT`:

> **No subprocess byte reaches a tool result without passing the ingress
> redaction.**

**The caps, with the numbers and the reasoning.**

| constant | value | why |
|---|---:|---|
| `OUTPUT_CAP_BYTES` | **1,000,000** (1 MB/stream) | the SAME number `sandbox.MAX_OUTPUT_BYTES` and `workspace.ResourceLimits.max_output_bytes` already used. A second number would have created "which cap applies" ambiguity at a security boundary. |
| `VERIFICATION_OUTPUT_CAP_BYTES` | **4,000,000** (4 MB/stream) | **the "do not truncate away the signal" case.** A truncated pytest traceback is not a diagnosis, it is a loop fixing a repository against evidence thrown away on the way to the model. `verify.py` runs the FULL SUITE as a regression run; `execution.feedback` and `execution.rationale` parse the same text for the failing test name and the assertion line. |
| `OMISSION_MARKER` | `[... N chars omitted ...]` | matches the marker the two existing collectors already emit, so one output cannot be produced by two cap mechanisms |
| `REDACTION_UNAVAILABLE` | `(output withheld: redaction unavailable)` | the fail-closed replacement |

**Cost, measured on this host (Python 3.10.11, win32) — the number that sets
the cap.** `redact_text` is linear since the R2-11 round:

| payload | `redact_text` |
|---|---:|
| 1,020 chars | **1.0 ms** |
| 100,000 chars | **78.2 ms** |
| 1,000,000 chars | **844.9 ms** |
| 4,000,000 chars | **3506.2 ms** |

So the cap runs **FIRST**, before redaction: a flood must not be able to
exhaust memory in the redactor, and capping first is the only way to make the
redactor's cost proportional to the cap rather than to the flood. 1 MB bounds
a hostile payload at ~0.85 s per stream and costs ~1 ms on a normal one. The
4 MB verification cap is unreachable for sandbox output (the collector was
already capped at 1 MB) — it is a bound on the LOCAL and `NATIVE_OS` paths,
which the sandbox cap never covered, and **making the verification cap LOWER
than the sandbox cap would have been the only genuinely dangerous choice.**

**Three properties, in the order they are enforced.**

1. **Cap → redact.** Enforced and proven: `bound_text` is exact over a
   **3,600-case grid** (every cap 0-299 × bodies 0-1,000,000) with **zero**
   overshoots. The first version overshot by 2 characters because it reserved
   room for the marker text but not for the two newlines around it — the same
   defect `harness/agent_loop`'s planner block shipped once.
2. **Redaction is DELEGATED.** `seal_output` imports and calls
   `shared.security.redact_text`. It does not own a pattern table. Gap G34
   (two redaction policies) is closed for the subprocess path.
3. **Fail closed.** A redactor that RAISES, or returns a non-`str` (including
   `None`), produces `REDACTION_UNAVAILABLE` and `IngressReport.ok=False`.
   Raw bytes are never passed through. Both branches are measured.

**Verification-signal preservation, measured.** A real container
(`python:3.10-slim`, Docker 28.5.1) running
`echo "TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"` returns
`TOKEN=[REDACTED_SECRET]`; the same container with `purpose="verification"`
returns `FAILED tests/test_a.py::test_b - assert 1 == 2` and
`=== 1 failed, 12 passed in 4.21s ===` **byte-identical**. Redaction removes
secrets; the cap, and only the cap, can cost signal — and it costs 4 MB of it
on verification.

**How the tag reaches the sandbox** is the `_boundary_names` rule again, and
for the same reason (this repo has already paid for a keyword forwarded on
permissiveness: 26 eval arms went red). `verify.py::_verification_output_kwargs`
forwards `purpose="verification"` only when the resolved boundary EXPLICITLY
names `purpose`; an uninspectable callable or a three-positional-arg
`harness/_stubs/sandbox` gets `{}` and the historical call.

### 2. Every egress, routed

| producer | file:line | route |
|---|---|---|
| sandboxed command (all 5 return sites) | `sandbox.py` `_sealed_result` | `seal_streams`; the collector is given the SAME cap so a raised verification cap is a boundary the capture actually had |
| local subprocess | `workspace.py` `LocalExecutionHandle.wait` | `seal_output` at result-build time; `output_bytes` keeps the TRUE byte count so a cap never shrinks a receipt |
| background process | `workspace.py` `BackgroundProcess.read_output` | only `text` is sealed; `next_offset` / `total_bytes` / `discarded_before` stay RAW so a redacted stream cannot make a caller re-read or skip a range |
| `git_*` tool dict | `workspace.py` `SafeToolBackend._git` | `seal_mapping`; `max_chars` is MODEL-SUPPLIED and unbounded above, so the ingress cap is the real ceiling |
| host test lane | `flake.py` `run_local_command` | `seal_streams` at the verification cap |
| warm container | `warm_sandbox.py` `_sealed` | delegates; no second policy |
| docker failure message | `sandbox.py` `_seal_error_text` | an exception message is an egress too |
| **`read` tool** | `workspace.py` `_read_tool` | **added this round** — see §3 |
| **`grep` tool** | `workspace.py` `_read_tool` | **added this round** — see §3 |

### 3. Two untrusted ingresses found by measuring, not by reading

The T2.W1.3 probe drove the real `SafeToolBackend`. Two things were open and
both are now closed, both squarely `execution/`'s own boundary:

1. **`read` returned a secret from a normally-named file.** The pre-existing
   fence was the WRITE path only (`apply_exact_edit` / `write` refuse
   secret-shaped content) plus a NAME-based refusal of `.env` / `*.pem` /
   `id_rsa*` / `credentials*`. So `read config.py` containing
   `API_KEY = "sk-live-..."` returned the key verbatim while `cat config.py`
   through the sandbox returned `[REDACTED_SECRET]` — **two fences on the same
   secret, and the weaker one on the path a model actually uses.** Now sealed,
   and **redacted rather than refused**: a refusal would break a test fixture
   with a fake key, and a boundary that cries wolf on fixtures gets worked
   around. The `ingress` receipt rides the tool result, so a redacted read is
   distinguishable from a clean one.
2. **`grep` was the highest-volume untrusted route AND unbounded.** It returns
   the matching LINES, so grepping for `password` returns exactly the lines
   carrying passwords, and the name-based refusal cannot help because the hit
   is in some other file. It also called `path.read_text()` with no size
   check, so one multi-gigabyte file made a `grep` allocate that much. Both
   fixed: hits are sealed per line, and the read is bounded by the workspace's
   own `max_file_bytes` before it happens.

**NOT fenced here, deliberately: prompt injection.** A file saying "ignore all
previous instructions" still reaches the model from the `read` path, from
`grep`, and from sandbox stdout. The authority is
`shared.security.review_untrusted_source`, which has API and coverage but **no
production call site for `issue` or `repository_instructions`** — the two gaps
`shared/AGENTS.md` records and this round's brief names explicitly. `shared/`
is Terminal 5's file. Requests U-1, U-3, U-4 below. The call sites are marked
in code with a bounded `UNFENCED` block so a reviewer sees them.

### 4. The untrusted-content table (T2.W1.3)

| # | execution-side untrusted ingress | status |
|---|---|---|
| U-a | sandboxed command stdout/stderr | **fenced** (secrets, this round) · **requested** (injection) |
| U-b | local / native-OS subprocess output | **fenced** (secrets, this round) · **requested** (injection) |
| U-c | background process output | **fenced** (secrets) · **requested** (injection) |
| U-d | `git_*` tool output (`git show` of arbitrary revisions) | **fenced** (secrets, this round) · **requested** (injection — a hostile repo crafts commit messages and blobs) |
| U-e | `read` tool file content | **fenced** (secrets, this round) · **requested** (injection) |
| U-f | `grep` tool matching lines | **fenced** (secrets, this round) · **requested** (injection) |
| U-g | test report → `raw_output` + `structured_feedback` → next turn | secrets **fenced**; injection **REQUESTED (U-1)** — the highest-severity open item: pytest prints test NAMES, the test's own SOURCE LINE and assertion messages, and `structured_feedback` is injected into the next turn as if harness-authored |
| U-h | `issue_text` → commit subject + PR body | secrets **fenced** (by a second local policy — U-2); injection **REQUESTED (U-3)**. **The only egress in the package whose output LEAVES the machine** |
| U-i | `result_parsing._output_text` | **DECLARED SAFE, with reason** — a VIEW, not an egress: consumed only by integer/boolean/crash extraction, never rendered. The real risk in this class is different and stated in the docstring: a test that PRINTS "10 passed" is counted as 10 passing tests. That is untrusted-INPUT-INTEGRITY (a forgeable measurement), answered in the right direction by `ecosystems.py`'s report-first parsing + fail-closed `no_tests_collected` — but the report channel is not declared for Python, so the prose count is still the authority there. Verifier-policy gap, not a boundary gap. |
| U-j | `snapshot.py` `git check-ignore` / `git rev-parse` | **DECLARED SAFE** — a repo-controlled `.gitignore` influences only which paths a snapshot plan KEEPS; every excluded byte is reported in the receipt, and the plan's own refusal (`plan_incomplete`) is fail-closed |
| U-k | `env_snapshot.py` `docker images` | **DECLARED SAFE** — a tag→id map of this product's own `harness-envsnap` prefix; no external content |
| U-l | `sandbox_adversarial` / `sandbox_stress` / `sandbox_sustained` `docker ps`/`images` | **DECLARED SAFE** — container names are ours (`hexec-*`) and the residue checks compare them against `own_container_filter()`; the only text is our own fixture names |
| U-m | `warm_sandbox` `docker exec` output | **fenced** (secrets, this round) · **requested** (injection). No production call site, which is exactly why it grew an un-fenced egress |

### 5. Cross-terminal requests to T5 (`shared/security.py` is yours)

These are written to be actionable without asking anything back. **None is
applied here.**

**U-1 (highest). Fence the verification report against injection.**
Call site: `execution/verify.py::_run_tests` → `parse_test_run` →
`VerificationResult.raw_output` and `result.structured_feedback`, which
`harness/agent_kernel` injects into the NEXT TURN as if harness-authored. Ask:
a `review_untrusted_source(..., source="test_output")` (or an existing source
label you prefer) applied to the capture, with the `structured_feedback`
objects built from the REVIEWED text rather than the raw one. Constraint to
respect: the review must not break `execution/feedback.py`'s and
`execution/rationale.py`'s parsers, which read the SAME text for the failing
test name and the assertion line — so the quarantine/bound has to leave
pytest's `=== N failed, M passed ===` summary and the `E   assert` lines
intact. `UntrustedPolicy`'s existing negation-scope suppression is what keeps
honest guardrail prose out of quarantine; a test report is the same shape.

**U-2. One redaction policy, not two.**
`execution/git_output.py::_redact_secrets` is a 50-line LOCAL regex table
(placeholder `[REDACTED]`, private-key replacement `[REDACTED PRIVATE KEY]`,
different coverage) and it carries the SAME two quadratic shapes the R2-11
round removed from the shared redactor (unterminated PEM with a lazy `.*?`;
unbounded `[A-Za-z0-9_-]{8,}` token run). It is used on `issue_text`, commit
messages and the PR body. Ask: either a shared `redact_for_publication`
entry point that returns the legacy `[REDACTED]` placeholder so
`tests/test_git_output_rationale.py`'s exact-string pins keep passing, or a
decision that the divergence is acceptable. **Not done here on purpose:** the
test pins the placeholder, and changing a commit message or PR body is a
visible contract change to a file whose tests are another owner's. The
docstring at `git_output.py:499` now says this out loud.

**U-3. Wire the `issue` source.**
Call site: `execution/git_output.py::produce_git_output(issue_text=...)` →
`_quoted_issue` (PR body) and `commit_message_from` (first sentence becomes
the commit subject). This is the only content in the product that LEAVES the
machine, and `issue_text` is the FIRST thing the agent ever sees. Ask: the
same `review_untrusted_source(text, source="issue")` the ceiling round
designed, applied before the text is quoted or a subject is derived. Note the
markdown angle: an unquoted issue body can close its own fence and forge a
`## Verification` heading that reads as harness-authored. A one-line prefix in
`_quoted_issue` would close that today and is a product decision, not mine.

**U-4. Wire the `repository_instructions` source.**
Call sites: `harness/context.py::discover_project_instructions` and
`harness/context_compiler.py` read `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` /
`QWEN.md` from the repository under repair. Measured this round: a hostile
`AGENTS.md` body reaches the model verbatim through the `read` tool. Those
files are `harness/`'s, so the call site is yours or T1's; the request is to
apply `review_untrusted_source(..., source="repository_instructions")` before
the body is compiled into a prompt, keeping the digest/citation contract that
`execution/snapshot.py` and retrieval already depend on.

**U-5. Tell me whether a redactor failure should be louder.**
`seal_output` returns `ok=False` and substitutes
`(output withheld: redaction unavailable)` — deliberately a VALUE, because a
redactor fault must not change a command's exit code (that would report a
machine fault as a code failure). But nothing in the product currently
SURFACES `ok=False`: `harness/trace.py` would be the natural place for a
`redaction_unavailable` trace row. Your call whether that is a row, a doctor
check, or deliberately silent; the receipt exists to be read.

**U-6. `INTERFACES.md` Change Log entry** (I did not edit it; not my file).
NEW `execution/ingress.py` (`OUTPUT_CAP_BYTES` 1 MB,
`VERIFICATION_OUTPUT_CAP_BYTES` 4 MB, `PURPOSE_VERIFICATION`,
`REDACTION_UNAVAILABLE`, `IngressReport`, `seal_output`, `seal_streams`,
`seal_mapping`, `cap_for`, `bound_text`, `INGRESS_INVARIANT`); NEW
`execution/containment_audit.py` and `execution/sandbox_cost.py`; and FIVE
additive keyword-only parameters on the Boundary-1 boundary:
`execute_sandboxed(..., purpose="", secrets=())`. **No signature was removed
or renamed, no event kind, serialized field, completion status, exit code or
verifier mint changed, and `shared/types.py` was NOT edited.** New additive
keys on existing tool results: `ingress` on the `read` result and on
`BackgroundProcess.read_output`'s two stream dicts; `ingress` on the `git_*`
result dict. `LocalExecutionHandle.ingress_report()` is a new read-only
accessor.

### 6. The 7-row containment attack table (T2.W1.2)

`python -m execution.containment_audit` — NEW, not in pytest (it needs the
daemon and 90 s of real containers per row). **Real Docker 28.5.1, this
tree, 2026-10-01: 7 claims, 7 HELD, 0 findings, 0 blocked, exit 0.**

| # | claim | attack | outcome |
|---|---|---|---|
| 1 | `.git` read-only inside a writable workspace | `touch .git/pwned` + `echo forged >> .git/config` from inside the container, then stat BOTH ON THE HOST | **HELD** — control workspace write landed; `.git` write refused in-container (rc=1, rc=1); nothing appeared on the host |
| 2 | writable roots cannot overlap read-only paths | `writable_roots=['.', '.git', '.git/refs']` | **HELD** — read-only won, 2 `overlaps` notes + 1 `refused` note, the pre-spawn argv gate ACCEPTED the policy's own argv, and a real container still refused the write |
| 3 | network is off unless declared | TCP to 1.1.1.1:443, 8.8.8.8:53, bridge gateway; loopback control | **HELD** — all three refused, loopback CONNECTED (control), receipt names `network_enabled=False` and the declaration |
| 4 | a non-existent read-only path is not mounted | `readonly_paths=['.git', '.not-a-real-dir']` | **HELD** — only `.git` mounted, `readonly_absent=['.not-a-real-dir']`, host dir never created |
| 5 | a fresh container per call | `/tmp` marker in call 1, bind-mount marker in call 1; look in call 2 | **HELD** — call 2 saw the bind mount (`BIND=PRESENT`) and not the `/tmp` marker (`TMP=GONE`) |
| 6 | resource limits are real | 2 GB alloc vs `--memory 1g`; fork bomb vs `--pids-limit`; 4 spinners vs `--cpus 1.0` | **HELD** — OOM-killed at exit **137**; fork bomb collapsed without reaching the timeout; 4 spinners took **2.32x** the 1-spinner wall |
| 7 | absolute / `..` / empty writable roots dropped, not clamped | 9 hostile declarations | **HELD** — all 9 dropped with a note, resolved set `[]`, workspace NOT flipped read-only by the refusals |

**The degradation is honest and PROVEN, not asserted.** With the daemon
simulated unreachable, the same run reports **6 rows `blocked` carrying the
exact daemon error** (`error during connect: Cannot connect to the Docker
daemon at unix:///var/run/docker.sock...`), **1 Docker-free row still `HELD`**,
`summary.verdict == "BLOCKED"`, `summary.clean == False`, and CLI exit **2, not
0**. There is no `skipped` state in the module at all: a row either attacked
something, refused to attack it and said why, or found a hole. The CLI returns
non-zero on a finding AND on a blocked lane, so a blocked lane cannot render
as a clean run.

**Three of the four findings this audit first reported were bugs in the AUDIT,
not in the product, and that is the controls earning their keep:**

1. Row 2 attacked `writable_roots=['.']` believing it "contains `.git`". It
   does not hit the conflict table: `.` normalises to the EMPTY relative path
   and is dropped as unusable. The implemented overlap direction is "a
   writable root INSIDE a read-only path", so the attack now declares
   `.git` and `.git/refs` too and requires a note for each shape.
2. Row 3's loopback control connected to **port 9** — a CLOSED port, which the
   local host refuses whether or not a network namespace exists. It proved
   nothing and correctly reported its own control as failing. The control now
   binds a real listener on an ephemeral port and connects to it.
3. Row 3 read `"EXT=" in facts` where `facts` is keyed by `"EXT"` — a string
   that can never be a key, so the row reported "the probe did not run"
   against a probe that had run and printed four facts. A gate that is wrong
   in the REFUSING direction is still a gate that cannot be trusted.

**Non-vacuity is structural.** Every Docker row asserts `attack_ran` AND a
`control_ran` arm: a workspace write that must land, a loopback connect that
must succeed, a bind-mount file that must persist. A refusal that fires
because the container is broken is not a containment proof, and row 6's fork
bomb is recorded as `container exit 0` **with the note that the surviving
shell runs the trailing echo** — "did not time out" is the assertion, not the
exit code, which is the same lesson Round 6 of this module recorded.

Row 6 found **no** new defect. Row 2's real finding was that the *pre-spawn
gate is a second, independent authority*: it ACCEPTS the policy's own argv
when told the writable roots, which is a property worth having recorded
because a policy that resolved correctly while its argv was refused would mean
the two halves disagree.

### 7. Sandbox cost, measured (T2.W1.4)

`python -m execution.sandbox_cost` — NEW. **Real Docker 28.5.1, warm image,
Windows host, Python 3.10.11.**

| # | measurement | value | note |
|---|---|---:|---|
| 1 | container creation, **cold** (first call, warm image) | **5.81 s** | a fresh `--rm` container per call is the design, so this is a per-call cost, not a startup cost |
| 1 | container creation, **warm** (median of 2) | **3.61 s** | samples 3.79 / 3.43 |
| 2 | per-call overhead vs a local subprocess | **+3.21 s** | sandboxed median 3.45 s vs local median 0.24 s; ratio **14.2x** |
| 3 | does a code edit trigger an image build? | **NO — 0 rebuilds** | tag `harness-exec:c72062d9dd61` IDENTICAL before and after editing 4 source files incl. a new package; `ensure_image` 0.469 s warm vs 0.347 s after |
| 4 | filesystem walk on the per-call path | **0 directory entries, 0.0000 s** | bar is ≤1,000 entries / ≤1 s — **within bar by three orders of magnitude** |

**Measurement #4 is the one that changes a conclusion, and it is the opposite
of the suspicion.** The bare-walk baseline on this tree is **526,400 entries
in 489.03 s** (measured with the brief's own one-liner), and
`execution/AGENTS.md` records a 362 s figure for the same walk. Neither is on
the per-call path: `ensure_image` hashes ~9 manifest FILES, and
`resolve_containment` / `readonly_overlays` stat a handful of directories. The
expensive walks are `execution/snapshot.py` (per-TASK) and
`harness/retrieval.py` (per-STEP), and this round did not add a walk to
either. **No per-call walk exceeded the bar, so nothing needed removing or
capping** — and the probe that would have caught one is in the module.

Measurement #2 is a DELIBERATE DIFFERENTIAL: the host's own process-spawn
cost is tens of ms on Windows and single-digit on Linux, so an absolute number
would measure the host rather than the boundary. Both arms run the same trivial
command through the shape the product pays.

Measurement #3 uses TWO observations because either alone is weak: the tag is
identical (the fingerprint ignores source) AND `ensure_image` costs the same
after the edit (no build was kicked off, which a tag collision could hide).

### 8. Verification actually run (this tree, `-p no:randomly`, real Docker 28.5.1)

- `tests/test_sandbox.py tests/test_verify.py tests/test_workspace_security.py`
  -> **142 passed** (463.01 s) — the required lane.
- `tests/test_ceiling_r2_02_flake.py tests/test_workspace_security.py` ->
  **94 passed** (104.34 s) — the required lane.
- `tests/test_tool_protocol.py tests/test_agent_kernel.py
  tests/test_ceiling_r2_07_codemod.py tests/test_editor_prompts.py` ->
  **181 passed, 1 skipped** (228.38 s). These are the lanes that pin the
  `read` tool result SHAPE, which this round changed (an additive `ingress`
  key); the skip is a Windows symlink-privilege case, not a pass.
- `tests/test_git_output_rationale.py tests/test_ceiling08_verification.py
  tests/test_feedback.py tests/test_ceiling_security.py` -> **174 passed**
  (288.38 s).
- `tests/test_e2e_run_task.py tests/test_verify_js.py
  tests/test_verification_gate_wiring.py` -> **95 passed** (1575.21 s). The
  whole verified-fix loop through a real container, the real JS dependency
  volume, and the real verification-intelligence gate.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0. No prompt changed.
- `python -m ruff check execution` -> **All checks passed** (was 36 errors
  before this round; this round's own files were clean on the first run and
  the remaining 5 pre-existing findings in `env_snapshot.py` / `feedback.py`
  were fixed — an unused import, an import sort, an `__all__` sort and a
  `SIM114` branch merge, all semantics-preserving and diffed).
- `python -m compileall -q execution` -> exit 0.
- `git diff --check -- execution` -> **exit 0**.
- `python -m execution.containment_audit` -> **7 claims, 7 HELD, 0 findings,
  0 blocked, exit 0.**
- `python -m execution.sandbox_cost` -> 4 measurements, **exit 0**, no
  blocked row.
- `docker ps -a --filter name=hexec-` -> **0 rows** after every run above: no
  container residue from this round.
- **No live-provider lane was run** and none is claimed; no credential was
  inspected, requested or retained.

### 9. What is NOT implemented, and what I deliberately did not do

- **Prompt injection is unfenced everywhere.** Every one of the ten request
  rows above needs `shared/security.py`, which is T5's. This round closed the
  SECRET half on every ingress it owns and marked the injection half in code
  so a reviewer sees it. A repo whose `AGENTS.md` says "ignore previous
  instructions" can still speak to the model, and the audit table says so
  rather than implying otherwise.
- **`git_output._redact_secrets` is still a second redaction policy.** Not
  removed: `tests/test_git_output_rationale.py` pins its `[REDACTED]`
  placeholder, and a commit message / PR body is a visible contract. Filed as
  U-2. The docstring now says out loud that it is a second policy, so the next
  reader does not have to rediscover it.
- **The PR body is not markdown-fenced against issue text.** An unquoted
  issue body can forge a `## Verification` heading. A one-line prefix in
  `_quoted_issue` would close it; it changes a pinned output shape and the
  forge-facing format is a product decision.
- **`execution/warm_sandbox.py` still has no production call site** and still
  has no containment (the AGT-07 gap). Its ingress IS wired now, so the two
  gaps are separable: the containment one is still open.
- **The container-egress exposure is unchanged**: `allow_network=True` reaches
  whatever the default bridge offers. Row 3 proves the DEFAULT is off and that
  the receipt names the declaration; it does not make the bridge filtered.
- **The unquotaed bind-mount disk exposure is unchanged** (Round 6 design
  finding, accepted).
- **The audit does not run in pytest.** It needs a live daemon and ~90 s of
  real containers per row; it is a CLI driver like `sandbox_adversarial.py`,
  which is the existing precedent for this class. T5's test intent for it is in
  the handoff.
- **58 `harness-exec` images** are on this host's daemon after the test lanes
  (each fixture repo gets its own fingerprint tag). That is the documented
  per-repo behaviour, not a leak, and I did not prune a shared daemon's cache.
- **No `harness/config.py` key was added**, so nothing switched silently. The
  caps are module constants in `execution/ingress.py` with the reasoning in
  their docstrings, which is the same convention `execution/sandbox.py`'s own
  `MAX_OUTPUT_BYTES` already used.
- **The structural LOC measure is approximate.** `git diff --numstat` is NOT a
  measure of this round: four terminals are editing the same tree, so every
  file under `execution/` already carried other terminals' uncommitted work
  before this session. The exact figure is the three new files
  (**2,148 lines**); the ~112 structural code lines added to seven edited
  files are counted from the marked call sites, and a large share of the
  edited-file additions are the `UNFENCED` / `INGRESS` comment blocks the brief
  requires at each call site.

---
## AGT-07 — axis (a): the writable root is no longer a writable repository (2026-09-28)

**`execution/sandbox.py` only. `verify.py`, `workspace.py`, `warm_sandbox.py`
and every other execution module were NOT edited. `ExecutionResult` and the
Boundary-1 signature are unchanged; the three new `execute_sandboxed` keywords
are keyword-only with safe defaults.**

### The defect

The container already had `--network none`, a read-only rootfs, dropped
capabilities and a non-root user. The workspace bind mount, though, was
unconditionally **read-write**, so "the agent may edit files" also meant "the
agent may rewrite `.git`" — which is the one tree whose forgery changes what a
reviewer believes happened, and the harness's own diff/patch path reads it as
repository content. There was no name for "what this container may touch", so
the only available question was "did anyone ask".

### What landed

| surface | what it owns |
|---|---|
| `ContainmentPolicy` | the declared boundary: network, read-only subtrees, writable roots, notes. `to_dict()` is the axis-(a) receipt and carries NO prompting field |
| `resolve_containment` | resolves the policy; drops unusable paths with a note, refuses a writable root that overlaps a read-only path, distinguishes `None` (module default) from `[]` (real opt-out) |
| `readonly_overlays` / `writable_overlays` | the `(host, container)` mount pairs, for EXISTING paths only |
| `containment_receipt` | the receipt with the MEASURED `readonly_applied` set and the `readonly_absent` remainder |
| `execute_sandboxed(..., containment=, readonly_paths=, writable_roots=)` | applies the policy, re-checks the argv, records the receipt on the trace event |

`DEFAULT_READONLY_SUBPATHS = (".git", ".hg", ".svn", ".bzr", "_darcs",
".vex")` is mounted READ-ONLY **inside** the writable workspace: the overlays
are appended after the workspace mount, and Docker applies a deeper target over
a shallower one, so `/workspace` stays writable for edits and `/workspace/.git`
is read-only. Declaring `sandbox_writable_roots` inverts it: the workspace
mount becomes `:ro` and only the declared subtrees are `:rw`.

Three properties are the ones a future edit must not undo:

1. **An absent read-only path is not mounted.** `docker run -v` CREATES a
   missing host path, so mounting a declared `.git` in a copy that has none
   would silently add a directory to a user's repository. It is reported in
   `readonly_absent` instead.
2. **A writable root can never re-open a read-only path.** The conflict is
   recorded and the read-only side wins — a config typo must not be able to
   widen containment.
3. **A declared network is reported as a declaration, not an allowlist.**
   `resolve_containment` adds a note when `allow_network=True` is set with no
   `VEX_EGRESS_ALLOWED_HOSTS`, because the bridge is not filtered and the
   existing `declared_egress_allowlist` docstring already says so.

### The pre-spawn gate got STRICTER, and that is deliberate

`assert_sandbox_argv_isolated(args, repo_path)` gained
`*, writable_paths=()`; the two-argument call is unchanged. A mount whose
source is strictly inside the workspace is now permitted — it reaches nothing
the root mount does not already reach — and it must declare its mode, because
**`docker -v src:dst` defaults to READ-WRITE**. A containment overlay that
lost its `:ro` would widen the container, and this gate is the thing whose job
is to catch exactly that, so a mode-less or undeclared-`rw` in-workspace mount
is refused. A read-write in-workspace mount is admitted only under a declared
writable root.

### Verification (real Docker 28.5.1, this tree, `-p no:randomly`)

- NEW `tests/test_agt_07_two_axis_approver.py` → **37 passed**, of which three
  are real-container proofs: the workspace write lands and `.git` refuses
  (`GIT=READONLY` read off the container), the `readonly_paths=[]` control
  shows the same command writing `.git` (`GIT=WRITABLE`), and a real
  `sandbox_call` trace event carries the containment receipt on its own field.
- `tests/test_sandbox.py tests/test_ceiling_security.py
  tests/test_workspace_security.py` → **154 passed** (`test_sandbox.py` alone
  61 passed, 178.6 s; the three together 154 passed in 247.6 s).
- `tests/test_verify.py tests/test_verify_js.py` → **68 passed** (real Docker).
- `tests/test_e2e_run_task.py` → **28 passed** (real Docker, 591.9 s) — the
  whole loop still verifies through a container that now has a read-only
  `.git`.
- `python -m evals.run --check` → **14/14 CLEAN**. `ruff check` clean on
  `execution/sandbox.py`; `compileall` clean.

**One red result, honestly attributed:** a first combined
`test_sandbox + test_ceiling_security + test_workspace_security` run reported
`1 failed, 53 passed` AND then died with `MemoryError` inside pytest's own
failure repr (the host was loaded by parallel terminals). The same three files
re-run together passed **154/154**, and each file passed standalone. Recorded
as host pressure, not exonerated by memory.

### Not implemented / honest gaps

- **`execution/warm_sandbox.py` is NOT covered.** It calls `_docker_run_args`
  and `assert_sandbox_argv_isolated` without the containment arguments, so a
  warm-container task keeps the historical writable `.git`. The one-line fix is
  `readonly_mounts=readonly_overlays(resolve_containment(self.repo_path))`; the
  file is not in this round's ownership. **Please take it** — an unwarmed task
  and a warmed task having different containment is exactly the kind of drift
  this round is for.
- **Container egress is still not filtered** (unchanged, previously recorded).
  A declared network reaches whatever the bridge offers; the receipt says so.
- **`resolve_containment` reads `sandbox_readonly_paths` /
  `sandbox_writable_roots` from a `config` mapping, but nothing passes
  `Task.config` into `execute_sandboxed` today.** The keys exist in
  `harness/config.py` as `None`; the plumbing is `harness/agent_kernel/strategy.py`
  and `execution/workspace.py::SafeToolBackend` (both other files). Until then
  the module default (`.git` + VCS metadata read-only) is what runs, which is
  the safe direction.
- The dependency-**image build** still uses Docker's default build network.
- **`harness/_stubs/sandbox.py` (the non-Docker local-subprocess fallback) has
  no containment at all** - it is a local process by construction, so a run on
  that stub has no `.git` boundary and no network boundary. It is not this
  round's file and the change would be meaningless (a local subprocess cannot
  have a read-only subtree), but it means a `--no-docker` / `HARNESS_USE_STUBS`
  run is UNCONTAINED, and the honest statement is that the two lanes are not
  equivalent. The daily path's `agent_process_sandboxed` default keeps the real
  sandbox in front of a human, so this is the test/dev lane, not the product
  lane.

## R2-10 — snapshots: ignore the generated, scope the monorepo, bound the disk, share the immutable (2026-09-26)

**NEW `execution/snapshot.py`. The run directory's `pristine` + `work` pair is
now a planned, measured, scoped, budgeted, shareable artifact instead of a
`shutil.copytree` of everything. Nothing is wired into production yet — the
call sites belong to other prompts and are written out in full at the bottom of
this section. That sentence is the honest state, not a caveat about a finished
thing.**

### The problem, measured on this repository

`editor.snapshot` had a fixed ignore set of seven directory names, `*.pyc` and
`.git`. It never consulted `.gitignore`. A real plan over this repo measured
**530.8 MiB across 16,408 files** that the repository itself declares generated
(`.harness/code-graph/`, `Temp/`, `logs/`, `graphify-out/`) and that the old
copier would have walked. Separately, `memory.paths.snapshot_disk_receipt` on
the real `logs/` root measures **155.5 MiB across 45 run directories, 151.6 MiB
of it `pristine/`** — the reference copy is ~98% of the artifact tree, and
`pristine` is the half that is never mutated. Both numbers are reproducible with
the commands at the end of this section.

### 1. The ignore decision is git's, not a second matcher

`git_ignored_paths` runs **one** `git check-ignore --no-index -z --stdin`
subprocess for the whole tree, after a `git rev-parse --show-toplevel` probe.
Three details are load-bearing:

- **`--no-index` is required.** Without it git refuses to report a TRACKED path
  that also matches an ignore pattern. The question being asked is "does the
  repository declare this generated", not "is it in the index".
- **The toplevel probe is required, and my own test caught why.**
  `git check-ignore` outside a repository exits **1 with no output** —
  byte-identical to "nothing is ignored". The first version read that as a real
  answer and reported `ignore_source=git+generated` for a plain directory git
  never looked at. Now a missing toplevel returns `None`, the fallback is
  `execution.workspace.is_generated_path`, and the receipt says which ran.
- **The query is rebased onto the work tree's top level**, so a source that is
  a subdirectory of a repository inherits that repository's rules and the answer
  is mapped back. Pinned by
  `test_a_source_that_is_a_subdirectory_of_a_repo_uses_that_repo_rules`.

`IgnoreRules.excludes` treats a directory match as covering its whole subtree,
so one `node_modules/` entry prunes the subtree rather than matching every
descendant. When git cannot answer, `source` is `generated` and `note` explains
why. A raising probe is a recorded degradation, never a crash.

**The reuse answer, concretely:** this module owns NO pattern table. It reuses
`execution.workspace.is_generated_path` — one additive public function added to
that file this round, the non-raising walk-time form of the `_is_artifact` set
`is_protected_path` already uses. `is_protected_path` is a *refusal* API that
raises on an absolute/traversing path, which is right for one tool call and
wrong for a copier walking a hostile tree.
`tests/...::test_the_ignore_matcher_is_git_and_not_a_second_implementation`
reads this module's source and fails if a pattern table ever appears.

### 2. `TaskScope` — declaring "this task is about services/api"

Config key **`task_scope`** (one relative path or a sequence). Opt-in by key
presence: absent / `None` / `False` / `""` / `[]` all resolve to the WHOLE
repository, because an absent key must leave every existing run byte-identical.
Absolute, drive-relative, traversing, whitespace-only, non-string, and
non-existent values **raise** `SnapshotScopeError` — a scope that silently
degraded to "everything" would snapshot the full tree while the operator
believed they had scoped the run.

**The one documented exception, and why it is not optional.** A bounded set of
ROOT configuration files (`DEFAULT_SCOPE_CONFIG_FILES`: `pyproject.toml`,
`setup.py`, `setup.cfg`, `pytest.ini`, `tox.ini`, `noxfile.py`, `conftest.py`,
`requirements.txt`, `package.json`) is always carried into a scoped snapshot and
always reported as `SnapshotPlan.config_files`. Without them the scoped
`pristine` copy has no pytest table, so the scoped verifier runs a **different
suite from the baseline** — precisely the "is this still the same suite"
failure R2-03 was built to prevent. ROOT only: a `services/api/pytest.ini` is
ordinary scoped source. A whole-repository scope reports `config_files == ()`,
because the exception is not in force. `TaskScope.always_include=()` opts out.

`scope_verdict(scope, changed) -> ScopeVerdict` is the report: `in_scope`,
`escaped`, `respected`. An escape is not forbidden here (that is
`harness.editor.is_protected`, and it stays there) but it MUST be visible,
because a scoped run whose diff silently includes another package is a different
claim from the one the operator scoped.

`execution/test_selection.py` gained ONE additive keyword-only `scope=None`
which narrows the CANDIDATE TEST SET before any graph reasoning, so the
soundness argument is unchanged. An empty in-scope set gives
`strategy="scope_empty"` + `is_empty=True`, so the module's own
"zero selected tests is never a pass" fallback still fires. An explicit
`target_test` outside the scope is still selected and reported in
`out_of_scope`.

### 3. The budget — plan, enforce, then write

`plan_run_snapshot` walks and stats **without writing a byte**;
`create_run_snapshot` enforces `enforce_budget` and only then writes. A refusal
raises `SnapshotBudgetExceeded` carrying the `BudgetVerdict`, so the caller
renders the numbers instead of re-deriving them; `allow_budget_refusal=True`
returns the same verdict on the receipt for a caller that shows a message rather
than failing.

`BUDGET_REASONS` is a closed exported set: `within_budget`,
`over_operator_budget`, `below_reserve`, `over_budget_and_below_reserve`,
`free_space_unknown`, `plan_incomplete`. All six are refusals except
`within_budget`. Two of them are the ones a naive implementation gets wrong:

- **`free_space_unknown` refuses.** "Could not measure the volume" must not
  read as "plenty of room"; the whole point is to refuse rather than discover the
  disk is full halfway through a copy.
- **`plan_incomplete` refuses.** The planning walk is bounded
  (`snapshot_plan_budget_s`, default `DEFAULT_PLAN_BUDGET_S = 30.0`, and
  `MAX_WALK_ENTRIES = 400_000`). A walk that stopped early measured only part
  of the tree, so its total is a **lower bound**; certifying a ceiling against a
  lower bound is optimistic in exactly the direction this mechanism exists to
  stop. `overage_bytes` is `-1` there ("not computable") rather than `0`, and
  `render()` gives that reason its own sentence instead of an overage figure
  computed from a number known to be too small.

The ceiling is checked against the **pre-link** byte count, which is the upper
bound: content sharing can only reduce the real figure afterwards. A budget
checked against a post-sharing estimate would get looser precisely when sharing
works.

### 4. Sharing — safe, and honest when it is not

`SharedStore` is content-addressed: `<root>/blobs/<first-two-hex>/<digest>`.
`pristine/` entries are **hardlinks into the store and are chmod'ed read-only**.
`work/` is **always a private byte copy**, never a link.

That separation is the whole safety argument, and it is structural rather than
conventional. An agent edits through a **bind-mounted sandbox where a shell
redirect truncates in place**. If `work/` were linked, one `>` would silently
rewrite the diff baseline and every other run sharing the inode. Proven by
`test_the_shared_reference_is_read_only_and_the_write_path_is_private`, which
writes IN PLACE through `work/` and asserts (a) pristine and work are different
inodes, (b) pristine's `st_nlink >= 2`, (c) pristine and the stored blob are
byte-unchanged afterwards, and (d) an in-place write through `pristine/`
**raises** rather than corrupting.

`SharedStore.link` returns `(shared: bool, reason: str)` and falls back to
copying on a different volume, a filesystem without hardlinks, or a permission
denial. `SnapshotReceipt.shared_disabled_reason` names the reason and
`shared` stays 0 — nothing is ever reported as shared that is not. Blobs are
written to a unique temp file and `os.replace`d, so a crash mid-write cannot
leave a truncated blob a later run would hardlink as if it were the content.

**If you cannot make sharing safe, do not ship it.** The specific hazard that
would have made this unsafe is hardlinking `work/`, and that is not done.

### 5. Retention — removes what it claims, and nothing else

`prune_run_directories(log_root, settings, dry_run=, now=, store=)` and
`prune_shared_store(...)`. Narrowness is the contract: a directory is a
candidate only if `is_run_directory()` recognises it (it carries
`trace.jsonl`/`state.json`/`plan.json`, or a `pristine`/`work` pair), and every
other entry of the log root is reported in `PruneReport.skipped` and left alone.
`test_retention_pruning_removes_only_what_it_claims` puts sentinels beside the
runs — a plain `notes/`, a `session-index/`, the shared store, and a run inside
the keep-latest window — and asserts every one survives.

**Age + bytes + keep-latest compose, and the composition is not obvious:** a run
is removed only when the age window and/or the byte budget selects it AND it is
not among the newest `keep_latest`. `keep_latest` is therefore a floor, not a
preference: with `keep_latest=20` and a 45-directory root, 20 runs are never
candidates and the age window prunes the rest. Pinned both ways
(`test_keep_latest_protects_an_expired_run`).

`force_rmtree` is Windows-read-only safe: a read-only file cannot be deleted
there, and the policy would otherwise fail on precisely the shared entries it
exists to reclaim. The rmtree error-handler keyword (`onerror` vs `onexc`) is
chosen once by version, because the project supports 3.10-3.12. One locked
directory is reported in `PruneReport.errors` while the rest still prune —
swallowing it would be the same as reporting success; aborting would make the
policy unrunnable.

The policy shape is `shared.retention.RetentionPolicy`
(`RetentionSettings.to_policy()`), so there is one vocabulary in the tree
rather than a second one that reads the same and disagrees. Bounded defaults
live HERE, not in `DEFAULTS`: `DEFAULT_RETENTION_DAYS = 30.0`,
`DEFAULT_RETENTION_KEEP = 20`, `DEFAULT_STORE_MAX_BYTES = 8 GiB`, plus
`DEFAULT_RESERVE_BYTES = 512 MiB` and `DEFAULT_PLAN_BUDGET_S = 30.0`.

### 6. Visibility — `doctor`'s disk section and `memory.paths`

`memory/paths.py` gained the LOCATION authority and the read-mostly receipts:
`SNAPSHOT_STORE_DIRNAME`, `snapshot_store_root(log_root, repo)`
(`VEX_SNAPSHOT_STORE` wins; otherwise `<log_root>/_snapshot-store`, so content
is per-repository and one repo's retention pass owns its own store),
`retention_receipt`, `snapshot_disk_receipt`, and
`prune_snapshot_artifacts`. All four **lazily import `execution.snapshot`
inside the function** — `memory.paths` is a bottom-of-the-stack location
authority that must stay importable by anything, including from inside
`execution` itself. All four are total: an unimportable engine, an unreadable
root, or an absent directory produce a receipt with the reason in `error`, never
an exception. The store directory is deliberately NOT a run directory, so
retention pruning can never sweep the shared store away as if it were one task.

`cli/doctor.py` gained `check_snapshot_disk()` and the
`DoctorCheck("snapshot_disk", "disk", "Snapshot disk", ...)` row — the ninth
check. Its vocabulary is deliberate: `failed` only when something is wrong NOW
(free space under the reserve, or the measured tree over the ceiling); a large
tree that is within policy is `ok` with the numbers in its `evidence`, because
"you have 155 MiB of run directories" is information, not breakage; an
unmeasurable root is `error` with the reason, never a fabricated zero. The
remediation names a command that runs: `python -m execution.snapshot
--log-root <root> --dry-run`.

**One seam worth knowing:** `DoctorCheck.probe` is `Callable[[], Dict]` — a
zero-argument contract several tests pin — so the disk check reads module state
(`_ACTIVE_LOG_ROOT`) to learn the caller's log root. `run_doctor` sets it, calls
the new private `_collect`, and clears it in a `finally`, so a later unrelated
`/doctor` cannot report against a stale root
(`test_doctor_keeps_the_active_log_root_scoped_to_one_call`).

### 7. A performance defect my own measurement found

The first version measured the tree FOUR times (kept / ignored / out-of-scope /
carried) via four `_measure` passes. On this host a bare `os.walk` of the repo
already costs **362 s** over 291,174 files / 150,361 directories, so the plan
took over 10 minutes and a first live measurement attempt was killed by a
600 s command timeout. Two changes fixed it:

- **ONE walk, ONE `stat` per file** (`_Census`), with the per-file sizes kept so
  the post-walk git-ignore filter can re-total **without touching the disk
  again**. The git decision has to be batched (one subprocess for the tree), so
  a file under an ignored-but-not-generated directory is still *walked* — that
  cost is reported as `truncated`/`entries`, and it never affects the copy: no
  ignored byte is ever read or written.
- **`create_run_snapshot` reuses the plan's `in_scope` list** instead of
  walking a second time.

**Exclusions are narrow on purpose.** `_artifact_exclusions` prunes the RUN
directory (this operation is about to write there — the shape that produced a
live `RecursionError` in the interactive flow) and anything the caller names in
`exclude_roots`. It deliberately does **NOT** guess the enclosing log root: a
repository may legitimately commit a `logs/` directory of its own, and
`harness.editor.snapshot` already pins that a same-named directory which is real
repo content still copies. A caller whose log root really is inside the
repository passes it in — that is knowledge only the caller has.

### 8. Live measurement, this repository, this host

`plan_run_snapshot` over `.` with a 45 s planning budget:

```
entries walked    16621          truncated True
ignore_source     git+generated
kept              351 files -> 49.1 MiB for pristine+work
ignored           16408 files, 530.8 MiB  (never copied)
verdict           snapshot refused (plan_incomplete): the walk did not finish,
                  so 49.1 MiB is a LOWER bound and the none declared ceiling
                  cannot be certified; 24.2 GiB free, reserve 512.0 MiB
largest kept      Temp/opencode/scan-smoke{2,3,4}/_code-graph/.../graph.json
                  ~2.4 MiB each; Temp/product-round-harness-1/decisions.db 2.0 MiB
```

**The refusal is the finding.** This tree is pathological (a bare `os.walk` of
it takes 362 s), and the mechanism refused to certify a ceiling it could not
measure rather than reporting "within budget" about a walk that stopped early.
The named `largest` entries are the actionable part: the biggest things a run
would snapshot are **not** repository source, they are another terminal's scan
artifacts sitting in the working tree.

Reproduce both measurements with:

```powershell
python -c "import json;from pathlib import Path;from execution import snapshot as s;print(json.dumps(s.plan_run_snapshot(Path('.'), Path('probe/run-1'), config={s.SNAPSHOT_PLAN_BUDGET_KEY: 45.0}).to_dict(), indent=1)[:2000])"
python -c "import json;from memory import paths;print(json.dumps(paths.snapshot_disk_receipt(log_root='logs'), indent=1))"
python -m execution.snapshot --log-root logs --dry-run
```

### 9. What is NOT implemented

- **NOT WIRED. No production call site passes `config`/`scope`/`store` to
  `create_run_snapshot`.** `harness/editor.py::snapshot` is still the historical
  two-argument `shutil.copytree` and `harness/core.py:493-494` still calls it.
  Until the hand-off below lands, **production runs are unchanged and still pay
  the full 1.2 GB** — the mechanism is complete, callable, and measured, but
  inert. Nothing in this round pretends otherwise.
- **The retrieval wiring is a hand-off too.** `TaskScope.filter` /
  `TaskScope.outside` are the retrieval-facing contract and they are tested; the
  call site inside `harness/retrieval.py` is not mine.
- **No DEFAULTS entry.** Every knob is a key-presence opt-in read by
  `execution/snapshot.py`'s own bounded defaults.
  `test_no_snapshot_key_is_a_behaviour_changing_harness_default` FAILS if anyone
  adds one of them to `harness/config.py::DEFAULTS`, because a default is merged
  into every task and every eval arm at once. A `None`-valued discoverability
  entry would be behaviour-neutral; a real value would not be.
- **Sharing has no cross-run reference tracking.** Pruning the store uses blob
  mtime ("when this content was first stored") as the LRU approximation, not a
  true atime update per run. A blob still hardlinked by a live run's `pristine/`
  can in principle be pruned by the byte ceiling; the effect is a dangling link,
  not data loss, and the run's own `pristine` file survives (its inode lives as
  long as the link does). A reference-counted store is the correct fix and is not
  built.
- **No `site`/`build`-specific knowledge.** Excluding generated trees comes from
  the repository's own `.gitignore` plus the existing artifact set. A project
  that does not ignore its build output still gets it copied — which is
  semantically right (git does not consider it generated either) and means the
  saving is only as good as the repo's ignore hygiene.
- **No per-file-size or depth limit in the snapshot** (unlike
  `Workspace.max_file_bytes`). A 4 GiB checked-in blob is copied.
- **Retention is not scheduled.** `prune_run_directories` and the CLI exist;
  nothing calls them on a timer. That is a scheduling decision, not a gap in the
  mechanism.
- **No Docker lane and no live-provider lane were run.** The suite is host-only
  (real `git`, real `os.link`, real `rmtree`; no container, no model, no
  network), and neither unavailable lane is claimed.

### 10. Cross-terminal requests

**(1) `harness/editor.py` — R2-06 owns this file; I did not edit it. This is the
one change that makes the whole round live.** Make `snapshot` delegate, keeping
the two-argument call sites working unchanged and the new behaviour opt-in:

```python
def snapshot(src: str, dst: str, *, config: Optional[dict] = None) -> None:
    """<existing docstring> + options are Task.config keys; when config is
    None the historical whole-tree copytree behaviour is byte-identical."""
    try:
        from execution.snapshot import snapshot_working
    except Exception:
        <keep the existing copytree path verbatim>
        return
    snapshot_working(src, dst, scope=TaskScope.from_config(config or {}, src))
```

`create_run_snapshot` is the better target for the pair, because it is the one
that enforces the budget. `harness/core.py:493-494` wants:

```python
receipt = create_run_snapshot(
    task.repo_path,
    str(paths.log_dir),
    config=cfg,
    store=SharedStore(snapshot_store_root(log_root)),
    exclude_roots=[Path(log_root)],
    changed=changed_so_far,   # for the scope-escape report
)
trace.log("snapshot", receipt.to_dict())
```

`receipt.scope` / `receipt.budget` are what the prompt's "report the scope in
the receipt" asks for. A budget refusal should be a
`task_end status="error"` with `reason="snapshot budget exceeded"` and the
verdict's numbers, alongside the existing `except OSError` that already reports
a failed snapshot today.

**(2) `harness/config.py` — R2-04's file. The DEFAULTS decision.** Nothing from
this round should be added with a real value (see section 9). If you want
discoverability, a `"snapshot_budget_bytes": None` entry is behaviour-neutral
(`resolve_budget` already reads `None` as "no ceiling declared"). Please do not
add real-value defaults for the retention keys either: a default retention age
would immediately start deleting other terminals' run directories.

**(3) `harness/retrieval.py` — R2-10's scope must reach retrieval.** Narrow the
candidate set with the scope and report what fell outside:

```python
from execution.snapshot import TaskScope, scope_verdict
scope = TaskScope.from_config(cfg, repo)
verdict = scope_verdict(scope, retrieved_paths)
retrieved = list(scope.filter(retrieved_paths))
trace.log("retrieval_scope", verdict.to_dict())
```

`ScopeVerdict.escaped` is the reportable part: retrieval that returned a file
outside the declared scope should be visible, not silently dropped. "Respect the
scope" here means filter AND report, because a silently narrowed context is a
claim the model cannot check.

**(4) `harness/core.py` — the receipt belongs in the run's artifacts.**
`SnapshotReceipt.to_dict()` is JSON-safe. Write it to
`logs/{task_id}/snapshot.json` and add a `snapshot` trace row, so `vex status`
can show "this run was scoped to services/api and cost 49 MiB" without
re-deriving anything.

**(5) `tests/test_cli_power_tools.py` — a disclosed edit, please review.** Its
`TestDoctor::test_json_document_shape_is_stable` pinned
`summary.total == len(checks) == 8` and an eight-key set. The new disk check
makes that nine. I changed `8` to `9`, added `"snapshot_disk"` to the key set,
and ADDED a per-row assertion that every row carries exactly the seven fields
the two renderers read — so the pin is the same strength, not weaker. That is
the only test file outside this round's own suite that I edited.

**(6) `dashboard/collect.py` already skips `pristine`/`work`** and its comment
says they are ~90% of the logs tree. The R2-10 measurement puts that at 98% of
the bytes (151.6 of 155.5 MiB). If the dashboard ever wants to show artifact
weight, `memory.paths.snapshot_disk_receipt` is the authority — do not
re-derive it.

## VEX-CEILING-07 — the execution layer is the process-control half of recovery (2026-09-26)

**No `execution/` source file changed this round.** The abort guarantee in
ceiling prompt 07 ("a hard abort terminates a long-running child process within
five seconds") was already satisfiable by this module's existing cancellation
surface; what was missing was the HARNESS side that actually signals it. This
entry records that finding, because "we already had the primitive" is exactly
the kind of claim a future terminal should be able to check instead of
re-deriving.

### The two kill paths, and their MEASURED latency

`harness.core` / `harness.agent_loop` now pass a `CancellationToken` (from
`execution.workspace`) into `harness.tools.BashSession`. `BashSession.cancel()`
signals it, and from there:

- **Docker path** — `execute_sandboxed(..., cancellation_token=...)` already
  polled the token at **50ms** and called `_cleanup_container(proc, name)`
  (`docker kill` + `docker rm -f`, then terminate the local CLI process).
  **Measured this round: a real `sleep 300` container aborted in 0.39s,
  returning exit 130 with `timed_out=False`** — the documented cancel contract
  is real, not aspirational. Note `_cleanup_container` gives `proc.wait` up to
  30s in the worst case; the 50ms poll is what normally bounds it, which is why
  the harness's `abort_kill_deadline_s` (5) has ~4.6s of headroom over the
  observed 0.39s.
- **Local path** — `start_local_execution(...)` returns a
  `LocalExecutionHandle`; `handle.cancel()` closes the Windows Job Object (when
  one was allocated) and calls `_kill_process_tree` (graceful then forced, i.e.
  `taskkill /T` then `/T /F` on Windows, `killpg` SIGTERM then SIGKILL on
  POSIX). **Measured this round: a real `python -c "time.sleep(300)"` child
  aborted in 0.86s**, reported `cancelled=True` with exit 130.

Both are pinned by `tests/test_recovery_steering.py`
(`test_required_sleep_300_is_aborted_in_under_5_seconds_docker` and
`..._local`), and the Docker one self-skips with the daemon's reason when the
daemon is unreachable — a skip is BLOCKED, never a pass.

### What this means for the contract (no signature changed)

- `BashSession` forwards the token **only when the resolved sandbox's signature
  accepts it** (`cancellation_token`, `cancel_event`, or `**kwargs`).
  `execute_sandboxed` accepts it, so the fast path is live; a three-positional
  arg sandbox or a test double keeps working byte-identically. Nothing in
  Boundary 1 changed.
- Exit-code semantics are unchanged and stay distinct on purpose: **130 =
  cancelled, 124 = timed out.** The recovery policy keys `timeout` off
  `timed_out`/124 and the abort path off 130, so an abort can never be
  mistaken for a slow command and re-run with a bigger budget.
- `_capture_stream`'s head/tail byte collector was already explicit about
  omission (`[... N bytes omitted ...]`). The harness-side
  `shape_tool_output` in `harness/tool_errors.py` is the analogous control for
  the model-visible text and prefers the **failure tail** for pytest-shaped
  output; it is a harness concern, so it was not moved here.

### Blocked / not a pass

- The full `execution/` Docker selection (`test_sandbox.py test_verify.py
  test_verify_js.py test_git_output_rationale.py`) was **not** re-run by this
  round; its last recorded result is Terminal 2's own (142 passed). The Docker
  work here was one real `sleep 300` container kill plus the real
  `test_e2e_run_task.py` lane (28 passed).
- No live-provider lane. No credential was inspected or retained.

## VEX-CEILING-08 — verification intelligence and machine-checkable specs (2026-09-26)

**Five new modules. One rule: only the final gate can mint success, and it runs
the full suite.** Nothing in this round weakened a Boundary-1 contract, and
nothing silently fell back to a weaker one.

### What exists now

| module | what it owns |
|---|---|
| `execution/spec_ledger.py` | JSON spec artifact with IMMUTABLE items + a separate SHA-256 seal file and an item-attributing diff guard |
| `execution/test_selection.py` | stdlib-`ast` import-graph test selection for the inner loop, persisted with the run |
| `execution/result_parsing.py` | report-first verdicts (`pass`/`fail`/`timeout`/`no_tests`/`error`) with the evidence source recorded |
| `execution/flake.py` | clean-environment failure confirmation and an honest confirmation rate |
| `execution/independent_evidence.py` | held-out acceptance tests + a separate judge context, lucky-pass and tampering detection |
| `execution/verification_intelligence.py` | the gate that composes all five and owns the mint decision |

### 1. Machine-checkable specs

`spec.json` + `spec.seal.json` in a run directory. Each item carries `id`,
`title`, `acceptance`, `tests`, `passes`. The agent may flip `passes` and
nothing else.

- Immutability is **mechanical, not conventional**: the seal holds a SHA-256
  over the obligation projection and **excludes `passes`**, so honest progress
  reporting is free while a removed, added, or mutated item fails.
- The seal lives in its own file so a "helpful" rewrite of the spec cannot
  rewrite its own permission slip.
- `SpecGuardReport` names the offender (`removed` / `added` / `mutated`) so a
  refusal is actionable.
- An **unsealed** spec reports `ok=False, sealed=False` — never a pass.
- `apply_claims` raises on an unknown id instead of dropping it: inventing an
  obligation is as much a mutation as deleting one.
- Malformed artifacts (no id, no acceptance, no tests, duplicate ids, empty
  item list) refuse to load, so a broken spec can never read as "zero
  obligations met".

### 2. Incremental inner verification

`select_tests(repo, changed_files)` builds a repo-local import graph with
stdlib `ast` — no graph index, no tree-sitter build, no network, so it runs on
any user repository. A test is selected when it imports the changed module
directly or transitively; a changed file that IS a test selects itself.

- **Soundness over tightness.** No import match -> same-package tests -> ALL
  tests. `strategy` records which fallback fired (`import_graph`,
  `same_package`, `fallback_all`, `bounded`).
- The selection is persisted as `<run>/test_selection.json`.
- `inner_verify()` is the **explicit non-gating** entry point. An empty
  selection falls back to the full suite, because "zero selected tests" is
  never a pass.
- `verify(..., final_gate=True)` (the default) **ignores a supplied selection
  for gating** and always runs the full autodetected suite. A selection cannot
  weaken the final gate; that is test-pinned.
- `verify` gained three additive keyword-only params (`selection`,
  `final_gate`, `reports`). The return type is still
  `shared.types.VerificationResult`; `shared/types.py` was NOT edited and
  `harness/_stubs/verify.py` is untouched.

### 3. Independent evidence

`build_held_out_suite` materializes acceptance tests **outside** the repository
under test (enforced — it raises `ValueError` otherwise), ships its own
digest-protected `conftest.py` and `pytest.ini`, and **conceals** them while the
build loop runs (POSIX mode 0, plus a hard `PermissionError` from
`HeldOutSuite.read`).

- **Randomization is seeded ORDER randomization, not value synthesis.** An
  earlier iteration perturbed the EXPECTED value with the RNG; that would have
  made a correct implementation fail, so it was removed. Expectations are
  authored (`expr` + `inputs` + a templated `expect` formatted from the same
  values), and the seed shuffles case and input order reproducibly.
- The judge evaluates **its own copy of the repository** in a **short**
  system-temp staging directory. The shortness is load-bearing, not cosmetic:
  a deeply nested run directory makes Docker Desktop return
  `OSError: [Errno 5] Input/output error` on ordinary reads inside the mount,
  which reads exactly like a code failure. That cost a real debugging cycle
  and is recorded so nobody re-introduces a nested judge path.
- `gap_points = visible_pct - heldout_pct` is the reward-hacking gap; above the
  threshold it is reported as a lucky pass, never averaged away.
- Tampering detection compares per-file digests plus the directory listing; a
  missing pre-loop fingerprint is reported as a FAILURE, not as intact.

### 4. Flake-aware verification

`assess_failure` reruns a failing test in a **clean environment**: a staged
copy with `__pycache__`, `.pytest_cache`, `.coverage`, and order-dependence
markers purged, plus a scrubbed environment (no `PYTEST_*` / `VEX_*` /
`HARNESS_*`, no inherited `PYTHONPATH`).

- Classifications: `confirmed_regression` (actionable), `pre_existing_flake`,
  `transient_failure`, `unconfirmed`.
- **An unconfirmed failure never becomes an edit instruction.** `attempts=0`
  and a missing `run` both land in `unconfirmed`, honestly.
- `confirmation_rate` is `decided/total` — a run where every confirmation was
  skipped reports 0, not 100. `confirmed_rate` is `confirmed/decided`.

### 5. Robust result parsing

Evidence order is machine-readable report -> exit code -> prose, and the report
records which one won plus a confidence level.

- **Timeout is always its own outcome**, detected before anything else,
  including an exit 0.
- **A zero-test run is not a pass**: exit 5, a `0 passed` / `0 tests` count, a
  no-tests marker, an empty capture at exit 0, or `expected_tests >= 1` with
  nothing collected.
- **A crash-shaped capture is `error`, not "1 failed".** An unhandled
  `OSError` / `INTERNALERROR` with NO test counts is a broken run. This was
  found live: a Docker mount I/O error was being recorded as a failing
  acceptance test, which would have turned an environment fault into a fake
  regression. A capture that has real counts is still a result even with a
  traceback.
- A **renamed test collector** therefore cannot silently pass: renaming
  `tests/` produces exit 5 / zero collected / `no_tests`, and a vanished target
  node id produces a runner error. Both are test-pinned.

### The mint rule (structural, not advisory)

`VerificationOutcome.mint_success()` is true only when the final gate RAN, the
regression scope was `full`, and every MANDATORY gate passed. A skipped
mandatory gate counts as a failure. `final_result()` **raises** when the final
gate did not run, so a caller physically cannot read a subset result as a
verdict. A final gate that could not run (e.g. `SandboxUnavailableError`) is
`status="indeterminate"`, never a pass.

### Verification actually run (this tree, 2026-09-26)

- `python -m pytest tests/test_ceiling08_verification.py -q -p no:randomly` ->
  **78 passed** (74 host + **4 real Docker**). All seven required proofs are
  driven through the mechanism a real run uses.
- `python -m pytest tests/test_verify.py tests/test_verify_js.py
  tests/test_feedback.py -q -p no:randomly` -> **93 passed, 0 skipped** against
  the real Docker daemon. The `_result_passed` shim is byte-compatible with
  every previously pinned case.
- `python -m pytest tests/test_sandbox.py tests/test_e2e_run_task.py -q
  -p no:randomly` -> **88 passed, 1 failed**. The failure
  (`test_verified_success_state_complete_after_exhausted_turns`) is
  **pre-existing and not from this round**: it asserts `"exhausted"` in a step
  note, but Terminal 04's `ToolLoopGuard`
  (`harness/agent_kernel/tools.py:746`, untracked) stops the step first with
  `LOOP-GUARD: repeated identical command (3x)`. Not this module's file; not
  touched here.
- `python -m pytest tests/test_daily_driver_evals.py tests/test_evals_run.py
  tests/test_evals_tasks.py -q -p no:randomly` -> **69 passed**.
- `python -m evals.run --suite prompt-regression --check` -> **14/14 CLEAN**.
- Owned `ruff check` clean; `compileall` clean; scoped `git diff --check`
  clean.

Proof 4 (inner verification is materially faster) is measured with real
`python -m pytest` processes on the HOST through
`execution.flake.run_local_command` — an explicitly host-side lane, not a
Docker pass — against a 9-test fixture where 6 of the tests sleep 0.4s and the
selection picks 1 file. Proof 6 drives a real order-dependent flaky test. The
Docker-gated class drives the whole gate through `execute_sandboxed`.

### Known limitations, stated plainly

- The import graph is **name-resolution based and `ast`-derived**. Dynamic
  imports (`importlib`, plugin loading) and monkeypatched dependencies are
  invisible to it; that is why the same-package and all-tests fallbacks exist.
- The held-out suite's expectation values are authored by whoever writes the
  spec cases. Generation guarantees correctness and order randomization; it does
  not invent inputs.
- The judge's own copy is a full file copy. For a very large repository that is
  real disk cost, bounded by the task timeout.
- Concealment by POSIX mode is unavailable on Windows; there the flag plus
  `HeldOutSuite.read`'s hard `PermissionError` is what enforces it, and the
  suite lives outside the repository so the builder cannot reach it by path
  anyway.
- `run_local_command` is a HOST lane by design and is deliberately NOT wired
  into `verify()`. Production verification still goes through the Docker
  sandbox only.

## VEX-CEILING-09 — safe execution reuse (2026-09-26)

**NEW `execution/warm_sandbox.py`: one long-lived container per
`(repository, task)`, for the agent step loop only.** `execute_sandboxed`
remains the default and remains a FRESH container per call.

### The rules, and why each one exists

- **Identity is the reuse key, and it is enforced.** `SandboxIdentity` is
  `(canonical repo path, task id, image, purpose)`. A process-global slot
  holds the one live warm container; starting a second warm container with a
  different identity while one is live raises `IdentityMismatch` **rather than
  being satisfied by the existing one**. Files written inside task A's
  container therefore cannot be seen by task B's. `live_warm_identity()`
  exposes the claim for callers and tests.
- **The verification boundary is explicit and refused here.** `purpose` must
  be `"agent_step"`; `"verification"` (or anything else) raises
  `WarmSandboxError` at construction with the reason. The final verifier keeps
  a fresh container per command through `execute_sandboxed`, and
  `execution/verify.py` was **not touched** — the gate that mints verified
  success is unchanged and still per-command.
- **Per-command isolation remains available for hostile tasks.**
  `hostile=True` (or `reuse=False`) makes every `run()` delegate to
  `execute_sandboxed`, so no container is ever started and nothing is shared.
  The call site does not change shape; `ExecutionResult` is the same type with
  the same timeout (124 + `timed_out`) and cancellation (130) conventions.
- **The isolation flag set is identical.** The warm container is started from
  the SAME `_docker_run_args` builder with the SAME
  `assert_sandbox_argv_isolated` pre-spawn gate, so it carries `--network
  none` by default, `--read-only` + tmpfs `/tmp`, `--cap-drop ALL`,
  `--security-opt no-new-privileges:true`, memory/cpu/pids limits, a non-root
  user, `--pull=never`, and the same bind mount. The only difference is
  `docker run -d` plus an idle keep-alive command instead of the caller's
  command.
- **Names stay inside the existing reaping contract.** A warm container is
  `hexec-e<env8>-p<pid>-w<uuid>`, which still matches `own_container_filter()`
  and still parses through `_container_pid_from_name`, so a hard-killed
  owner's container is reaped by surviving peers exactly like a per-command
  one. `release()` stops and removes it, is idempotent, and clears the
  identity claim even when the stop fails (a wedged container becomes the
  orphan sweep's problem rather than blocking the next task).
- **Auditable.** `stats()` returns the identity, container name, exec counts
  for each mode, the creation time, and the declared egress allowlist; three
  trace-hook events fire (`sandbox_warm_start`, `sandbox_warm_exec`,
  `sandbox_warm_release`).
- **Fail-loud, like the rest of the module.** No daemon means
  `SandboxUnavailableError`, not a fallback to running on the host.

### Verification actually run (real Docker daemon)

- `tests/test_prompt_cache_cost.py::test_warm_task_sandbox_does_not_leak_files
  _between_tasks` — task A writes `/tmp/task-a-marker.txt` inside its own
  container; a second warm container for task B is **refused** with
  `IdentityMismatch` while A is live; after A releases, B's own container
  reports the marker absent (`test -e` exit 1) and still sees its own
  bind mount; the marker never reached the host.
- `::test_warm_sandbox_survives_repeated_commands_and_releases_cleanly` — four
  execs in one container, `stats()["warm_exec_count"] == 4`, the name starts
  with `own_container_filter()` and parses to this process's PID,
  `release()` returns True then False, and `docker ps -a` shows no residue.
- `::test_hostile_task_keeps_per_command_isolation` — a hostile sandbox runs
  its command, never starts a container, and reports
  `per_command_count == 1` / `warm_exec_count == 0`.
- `::test_warm_sandbox_argv_carries_the_full_isolation_flag_set` (no daemon) —
  the argv preview carries every isolation flag.
- `::test_warm_sandbox_refuses_the_verification_purpose` and
  `::test_warm_sandbox_fails_loud_without_docker` (no daemon).
- Regression after the change: `tests/test_batch_docs_lint.py
  tests/test_webfetch.py` -> **74 passed**; `tests/test_verify.py` /
  `tests/test_sandbox.py` / `tests/test_verify_js.py` /
  `tests/test_git_output_rationale.py` were not re-run this round (see the
  handoff's honest-blocked list).

### Honest notes

- A warm container keeps a cgroup's memory reservation alive for the whole
  task. That is a real cost under 40-50-way concurrency and is why reuse is
  opt-in per call site rather than the module default.
- `docker exec` runs a command in the container's own filesystem namespace, so
  state a command leaves OUTSIDE the bind mount (`/tmp`, installed packages)
  persists for the rest of the task. That is the point of the reuse, and it is
  also why a task that must not accumulate state uses `hostile=True`.
- This module is not wired into `harness.core`/`harness.agent_loop` by this
  round: doing so is a change to another owner's loop, and the step loop's
  scripted models already pin the tool-result shapes. The surface is complete
  and tested; the wiring is an explicit integration request in
  `logs/ceiling/terminal-09.json`.

## VEX-RELEASE-09 Git/CI hardening (2026-09-25)

- `git_output.py` now strips ambient `GIT_*` redirection and global/system Git
  config, disables filters, textconv, external diff, and fsmonitor hooks, and
  validates a clean index before producing output. Existing-repository failures
  restore the original branch and index; newly initialized failed repositories
  remove their `.git` directory.
- User-controlled branch names, commit text, diffs, issue text, and PR text are
  bounded/validated and credential-shaped values are redacted. Fixed-argv Git
  subprocesses never use a shell.
- `tests/test_git_output_rationale.py` covers real repositories plus hostile Git
  config/environment, filters, malformed branches, index restoration, cleanup,
  secret redaction, and diff rendering. Current focused result: **31 passed**.
- Release-side clean rooms require the current kernel terminal statuses
  `success` or `completed_verified`, with target/regression pass and non-flaky
  evidence. The exact current result remains a successful `TaskResult`.
- The prior Windows launcher-lock blocker is closed. Real installed-wheel
  verification now proves `vex.exe uninstall --yes` waits on the outer launcher
  process through a detached outside-interpreter helper, then removes both
  console entry points and distribution metadata. The clean-room gate remains
  strict and continues to invoke the real console entry point.

## Multi-language round (2026-09-21) — JS/TS sandbox + verify (undocumented until 2026-09-22 audit)

*(No AGENTS.md entry was written when this landed; reconstructed from the
working-tree diff on 2026-09-22. No behavior changed by this entry.)*

- **Sandbox (`sandbox.py`)**: `_detect_repo_language` (`js` when the repo
  is JS/TS-shaped, else `python`; Python markers win in mixed repos).
  JS/TS repos build a `node:22-slim`-based image (`harness-exec:node-base`,
  `BASE_IMAGE_NODE` overridable); npm deps install at BUILD time under
  `/opt/deps/node_modules` and mount read-only over
  `/workspace/node_modules` via a named volume (populated once per image,
  `_js_deps_volume`). Fingerprint covers `package.json` + lockfile
  (`JS_DEP_MANIFESTS`). The repo package is never npm-linked — tests
  exercise the bind-mounted source (same guarantee as pip).
- **Verify (`verify.py`)**: `_detect_language` + `_js_test_command`;
  Jest AND Vitest supported, target filter in Jest form
  `<file> -t <name>` (test-name substring filter, file as scope).
  Baseline/regression/three-valued-flake logic is language-independent
  and unchanged. Python pytest path byte-identical.
- **Tests**: `tests/test_verify_js.py` (unit: autodetect/target
  composition/fingerprint separation; Docker-gated: green/broken/
  regression/flaky on synthetic JS repos) + `tests/test_verify.py`
  still green for Python.
- **Scope note**: `project-spec.md` "explicitly out of scope" still
  lists multi-language support beyond Python — that line is now stale
  (see INTERFACES.md Change Log 2026-09-21 multi-language entry);
  the spec needs an amendment, flagged not silently rewritten here.

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

## Current session handoff (2026-09-24)

### Implemented and verified

- `verify()` now always performs the full-suite regression run when a
  target is supplied, even when an explicit command already embeds that
  target. A targetless verification runs the suite once; target reruns keep
  the documented pass/fail/timeout three-valued flake semantics. No-tests
  output is not accepted as success, and failing results carry structured
  feedback objects.
- Sandbox and verifier language detection now share one bounded source
  census and extension set. The set matches the structural graph's
  `.js/.jsx/.mjs/.cjs/.ts/.tsx` support. Valid JavaScript manifests must
  declare a runner; malformed or runner-less manifests fail closed.
- JavaScript dependencies use a cross-process, readiness-validated named
  volume mounted read-only. npm lifecycle scripts are disabled, alternate
  npm/Yarn/pnpm lockfiles participate in the fingerprint, and Vitest/Jest
  cache writes are disabled instead of weakening the dependency mount.
- Docker and Docker CLI output is drained through bounded head/tail
  collectors. Timeout and exception paths kill the named container and
  local Docker client; daemon connection failures raise
  `SandboxUnavailableError`. Invalid `HARNESS_SANDBOX_UID` values fail
  before interpolation.
- Git output stages only validated declared paths, disables ambient
  Git/system config and repository clean/smudge/process filters, rejects
  unsafe branch names, and does not commit undeclared files. Rationale
  parsing is malformed-input tolerant and requires explicit passing
  final-verification evidence before claiming success.
- Stress/adversarial/sustained drivers now enforce requested concurrency,
  scope residue checks to owned containers/processes, count killed-child
  canaries as findings, and use evidence-based fork-limit checks. The
  legacy performance probe no longer references removed pool/cache APIs.

### Measured results

- `python -m pytest -q tests/test_verify.py tests/test_verify_js.py tests/test_sandbox.py tests/test_git_output_rationale.py tests/test_feedback.py tests/test_multilang_graph.py --junitxml=C:\Users\pavan\AppData\Local\Temp\opencode\t2-unit-20260924-r4\pytest-unit.xml --tb=short` — exit 0, **158 passed, 41 skipped**. The skips are Docker-dependent and therefore **BLOCKED**, not passes.
- `python -m pytest -q tests/test_sandbox.py tests/test_verify.py tests/test_verify_js.py tests/test_git_output_rationale.py --junitxml=C:\Users\pavan\AppData\Local\Temp\opencode\t2-real-baseline-20260924-r3\pytest-baseline.xml --tb=short` — exit 0, **140 passed, 0 skipped** against the real Docker daemon.
- `python -m pytest -q tests/test_verify_js.py --junitxml=C:\Users\pavan\AppData\Local\Temp\opencode\t2-js-real-20260924-r2\pytest-js.xml --tb=short` — exit 0, **39 passed, 0 skipped**; real Node/Jest/Vitest dependency-volume lane.
- `python logs/dod/run_dod.py` — exit 0, **15/15 checks passed**; report is the terminal output and the generated `logs/dod/` artifacts.
- `python -m execution.sandbox_adversarial --out C:\Users\pavan\AppData\Local\Temp\opencode\t2-adversarial-20260924-r2\adversarial --timeout 90` — exit 0, **23 held, 1 confirmed-by-design, 0 findings**; report `C:\Users\pavan\AppData\Local\Temp\opencode\t2-adversarial-20260924-r2\adversarial\adversarial_sequential.json`.
- `python -m execution.sandbox_adversarial --concurrency 8 --rounds 1 --out C:\Users\pavan\AppData\Local\Temp\opencode\t2-adversarial-conc-20260924\adversarial --timeout 90` — exit 0, **8 held, 0 findings, 1/1 canary clean**; report `C:\Users\pavan\AppData\Local\Temp\opencode\t2-adversarial-conc-20260924\adversarial\adversarial_concurrent.json`.
- `python -m execution.sandbox_stress --tasks 24 --concurrency 12 --kill 5 --out C:\Users\pavan\AppData\Local\Temp\opencode\t2-stress-20260924-r2\stress --seed 23` — exit 0, all five driver checks passed, max 11/12 containers, zero residue, zero unexpected image growth; report `C:\Users\pavan\AppData\Local\Temp\opencode\t2-stress-20260924-r2\stress\sandbox_stress_report.json`.
- `python -m pytest -q tests/test_sandbox.py::TestSandboxIntegration::test_concurrent_cold_builds_share_one_image --junitxml=C:\Users\pavan\AppData\Local\Temp\opencode\t2-coldbuild-20260924\pytest-cold.xml --tb=short` — exit 0, **1 passed**; four independent processes converged on one cold image.
- `python -m ruff check` and `python -m ruff format --check` pass for all changed execution modules; scoped `git diff --check -- <T2 files>` passed. The unscoped shared-tree check is currently noisy because parallel terminals have unrelated installer edits.

### Blocked / not yet implemented

- Terminal 1's current status is `working` with 32 pre-change harness-suite
  failures, so the required T1 integration lane was not started. No
  competing T1 Docker workload was launched.
- The live-provider lane was not attempted; no credentials were inspected or
  retained.
- Poetry/pdm/conda dependency installation and an explicit per-call build
  network policy remain unimplemented. Runtime containers are still
  networkless by default, while the one-time dependency image build uses
  Docker's normal build network.
- The accepted `/workspace` bind-mount disk-consumption exposure remains;
  Docker has no portable per-bind quota on this host.
- `allow_network=True` is plumbed and audited in the sandbox trace and was
  exercised by the adversarial cross-container probe, but no live-provider
  test was run.

### Cross-terminal requests

- T1: align `harness/_stubs/verify.py` with the real verifier's regression
  rule for an explicit target embedded in `test_command`, and its targetless
  one-run rule.
- T4: update Boundary 1/project-spec documentation to describe the
  read-only JavaScript dependency volume, `--no-install` runner commands,
  `SandboxDependencyError`, and the current multi-language scope. No
  signature or shared-type schema change is required.

## Terminal 3 safe-workspace handoff (2026-09-24)

### Implemented

- `execution/workspace.py` is an additive host-side safety backend. It records
  canonical root/git identity and dirty paths, durable preimages, owner and
  lease IDs, pre/post hashes, proposed-effect hashes, atomic replacement,
  crash classification, operation/file/hunk undo, and stale-lease recovery.
- Exact edits require a unique match and can require an expected SHA-256 or
  revision. Paths reject traversal, shell-shaped names, VCS/artifact paths,
  symlink components, binary data, and secret-like content. Undo never
  overwrites a changed post-image and deletes agent-created files only after
  a matching hash check.
- `SafeToolBackend` labels read-only, mutating, local-process, sandboxed,
  remote, and MCP effects. Local execution is explicitly non-Docker, scrubs
  credential/harness environment variables, bounds output/runtime, supports
  cancellation and descendant cleanup, and exposes MCP child cancellation.
- `execution.sandbox.execute_sandboxed` now accepts additive cancellation
  tokens/events, polls for cancellation, returns exit 130, and scrubs secret
  variables from explicit container env input. Existing verifier calls still
  use the real Docker path and never fall back locally.
- `harness.editor` gained safe edit/write adapters and no longer follows
  symlink targets while comparing changed files.

### Verification

- `python -m pytest tests/test_workspace_security.py -q -p no:randomly` —
  exit 0, **20 passed, 0 skipped**; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-final2-workspace-20260924-20260924100347342\workspace-security.txt`.
- `python -m pytest tests/test_sandbox.py tests/test_verify.py tests/test_verify_js.py tests/test_git_output_rationale.py -q` —
  exit 0, **142 passed, 0 skipped**; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-final2-required-20260924-20260924100544657\required-pytest.txt`.
- `python logs/dod/run_dod.py` — exit 0, **15/15 checks passed**; console
  report `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-dod-20260924-20260924081314768\dod-report.txt`.
- `python -m execution.sandbox_adversarial` — exit 0, **24 attacks: 23 held,
  1 confirmed-by-design, 0 findings**; console report
  `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-adversarial-20260924-20260924081716461\adversarial-console.txt`,
  JSON `logs/sandbox-adversarial/20260924-134717/adversarial_sequential.json`.
- `python -m execution.sandbox_stress --tasks 50 --concurrency 50 --kill 7` —
  exit 0, all five driver checks passed, 43/43 non-killed and 7/7 respawned
  children clean, max 29 containers, zero residue; console report
  `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-stress-20260924-20260924082512111\stress-console.txt`,
  JSON `logs/sandbox-stress/20260924-135513/sandbox_stress_report.json`.
- `python -m pytest tests/test_editor_prompts.py -q -p no:randomly` —
  exit 0, **21 passed, 1 skipped**; the skip is symlink-host dependent and is
  not a Docker release blocker.
- `ruff check execution/workspace.py execution/sandbox.py execution/__init__.py harness/editor.py tests/test_workspace_security.py` —
  exit 0, clean.
- `ruff format --check execution/workspace.py execution/sandbox.py execution/__init__.py harness/editor.py tests/test_workspace_security.py` —
  exit 0, clean.
- `git diff --check -- execution/__init__.py execution/sandbox.py harness/editor.py execution/AGENTS.md` —
  exit 0, clean.
- `git diff --check` — exit 0 after parallel installer edits settled; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-final2-diff-20260924-20260924102101516\git-diff-check.txt`.
- `graphify update .` — exit 0, graph rebuilt with 12,716 nodes and 27,972 edges.

### Limitations and blockers

- The interactive kernel still needs an integration request to route its
  existing EDIT/WRITE/BASH calls through `SafeToolBackend`; this terminal did
  not edit `harness/agent_loop.py` per ownership rules.
- Local POSIX resource limits are applied when requested; Windows local
  execution relies on process-tree termination and timeout/output bounds,
  not a hard per-process memory quota. Docker remains the real verification
  isolation path.
- The existing Docker bind-mount disk-consumption exposure remains accepted;
  it is unrelated to workspace journal safety and is recorded by the
  adversarial report.
- No live-provider lane was run because no credential was inspected or used.
- The final unscoped shared-tree `git diff --check` is clean; owned Python
  files also pass their scoped checks.

## Final refresh (2026-09-24)

This refresh supersedes the earlier T2 session measurements above; the
Terminal 3 handoff remains intact.

- Final real-Docker required lane: `142 passed, 0 skipped`, exit 0, using
  `python -m pytest -q tests/test_sandbox.py tests/test_verify.py tests/test_verify_js.py tests/test_git_output_rationale.py`; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\t2-real-baseline-20260924-r7\pytest-baseline.xml`.
- Unit-only lane with `HARNESS_EXEC_SKIP_DOCKER=1`: `103 passed, 39 skipped`,
  exit 0; skipped Docker selections are BLOCKED, not passes; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\t2-unit-20260924-r6\pytest-unit.xml`.
- Current standalone JS lane: `40 passed, 0 skipped`, exit 0; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\t2-js-real-20260924-r4\pytest-js.xml`.
- Current DoD: `15/15`; current sequential adversarial: 24 attacks, 23 held,
  1 confirmed-by-design, 0 findings; current concurrent adversarial: 8 held,
  0 findings, 2/2 canaries clean; current stress: 12 tasks at concurrency 6
  with 3 hard kills, all 5 checks passed, max 6/6 containers, zero owned
  residue. Reports are under the corresponding
  `C:\Users\pavan\AppData\Local\Temp\opencode\t2-adversarial-20260924-r3`,
  `t2-adversarial-conc-20260924-r2`, and `t2-stress-20260924-r4` directories.
- The cold-build and orphan-reap regressions pass after removing global image
  deltas and warming the orphan fixture image; a transient shared-daemon
  failure was reproduced, fixed, and the final required lane passed.
- Daemon errors now invalidate the positive Docker-availability cache, and
  stress reports distinguish expected image fingerprints from unrelated
  global image growth. Production `ruff check`, production
  `ruff format --check`, `compileall`, scoped `git diff --check`, and
  `graphify update .` all pass.
- Test-file `ruff format --check` remains non-clean for pre-existing style;
  test `ruff check` is clean. Unscoped `git diff --check` remains blocked by
  unrelated parallel installer edits.
- T1 is still `working` with its 32 pre-change failures; T1 integration was
  not launched. A live `hexec-*` census observed during a parallel run was
  attributed to T1-owned PIDs, not T2; the latest global census still shows
  one such T1 container, so it was left untouched. T2's own checks left no
  owned residue.
- Installed-artifact check: built `vex_harness-0.2.1` into an isolated temp
  virtualenv, installed the wheel, ran imports and `python -m execution.sandbox
  --help` from outside the repository, and completed a real Docker smoke
  command successfully. An outside-cwd pytest run with an installed-path
  guard passed `103` tests with `39` Docker skips; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\t2-installed-artifact-20260924\pytest-installed-unit.xml`.
  The build emitted existing setuptools warnings for
  `cli.fixtures.smoke_repo`; it exited successfully and no packaging files
  were changed.

## Terminal 3 final correction (2026-09-24)

- Final required lane remains `142 passed, 0 skipped`, exit 0; the final
  workspace security lane is `20 passed, 0 skipped`, exit 0. Reports are
  `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-final2-required-20260924-20260924100544657\required-pytest.txt`
  and `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-final2-workspace-20260924-20260924100347342\workspace-security.txt`.
- Final sequential adversarial rerun is `24 attacks, 23 held, 1 confirmed-by-design,
  0 findings`, exit 0; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-final2-adversarial-20260924-20260924102639263\adversarial-console.txt`.
- Final 50-way stress rerun is BLOCKED by host `WinError 1455` (paging file
  too small): exit 1, 4/5 checks passed, 37/43 non-killed children clean,
  7/7 respawned children clean, max 33 containers, zero residue; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-final2-stress-20260924-20260924103204669\stress-console.txt`.
  The preceding clean run of the identical command passed all five checks;
  both results are retained rather than treating the environment failure as
  a product pass.
- Final unscoped `git diff --check` is clean, exit 0; all owned Python files
  pass ruff check and format check. The live kernel integration request and
  the paging-file stress blocker remain the only release handoff blockers.
  Report: `C:\Users\pavan\AppData\Local\Temp\opencode\product-round-terminal-3-final3-diff-20260924-20260924104205379\git-diff-check.txt`.

## Terminal 3 continuation (2026-09-24)

- The new `harness/agent_kernel` partial integration was validated without
  Docker: `python -m pytest tests/test_agent_kernel.py -q -p no:randomly`
  exits 0 with **18 passed, 0 skipped**; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\terminal-3-kernel-20260924-20260924104524745\pytest.txt`.
- `AgentKernel` now constructs `SafeToolBackend` and routes edit/write calls
  through it, but `harness/agent_kernel/strategy.py:821` still invokes
  `start_local_execution` directly for shell/process. Terminal 1 must make
  the process path explicitly choose `SafeToolBackend.execute(..., sandboxed=...)`
  so the backend label, cancellation, env policy, and Docker boundary are
  authoritative. Do not route it through an untyped fallback.
- The stress failure is confirmed external: the final report has six child
  exit-2 results, a monitor-thread `WinError 1455` while spawning
  `docker ps`, and zero owned residue. The host had only ~0.5 GiB free RAM
  and ~0.6 GiB free virtual memory during collection; no product change is
  justified from this evidence.

- Final non-Docker quality checks are clean: ruff check exit 0
  (`C:\Users\pavan\AppData\Local\Temp\opencode\terminal-3-ruff-check-20260924-20260924142940260\ruff.txt`),
  ruff format exit 0
  (`C:\Users\pavan\AppData\Local\Temp\opencode\terminal-3-ruff-format-20260924-20260924142826863\ruff-format.txt`),
  py_compile exit 0
  (`C:\Users\pavan\AppData\Local\Temp\opencode\terminal-3-compile-20260924-20260924142900089\compile.txt`),
  and unscoped `git diff --check` exit 0
  (`C:\Users\pavan\AppData\Local\Temp\opencode\terminal-3-final2-diff-20260924-20260924143913448\git-diff-check.txt`).
- `graphify update .` exits 0; current report is 12,783 nodes, 28,192 edges,
  2,558 communities, with console report
  `C:\Users\pavan\AppData\Local\Temp\opencode\terminal-3-graphify-20260924-20260924143055472\graphify.txt`.

## Execution verification closure (2026-09-24)

- Fixed the T1-reported verifier false negative: successful summaries such
  as `10 passed`, `20 passed`, and `100 passed` are no longer mistaken for
  `0 passed`. The zero-count detector is digit-boundary-aware and accepts
  only same-line horizontal whitespace, so unrelated output lines cannot
  fabricate a no-tests marker.
- Added unit regressions for multi-digit pass counts, explicit zero-count
  summaries, and cross-line output. No harness/runtime files were edited and
  no interface or schema changed.
- Final host lane: **109 passed, 39 Docker selections blocked/skipped**;
  report `C:\Users\pavan\AppData\Local\Temp\opencode\t2-closure-20260924-final2-unit.xml`.
- Final real-Docker lane: **148 passed, 0 skipped**; report
  `C:\Users\pavan\AppData\Local\Temp\opencode\t2-closure-20260924-final2-docker.xml`.
- The two T1 real-Docker scenarios that previously failed on the substring
  bug now pass (**2/2**); report
  `C:\Users\pavan\AppData\Local\Temp\opencode\t2-closure-20260924-final-t1-regression.xml`.
- Final DoD: **15/15**. Sequential adversarial: **24 attacks, 23 held, 1
  confirmed-by-design, 0 findings**. Concurrent adversarial: **8 held, 0
  findings, 2/2 canaries clean**. Stress: **12 tasks / width 6 / 3 kills,
  5/5 checks, 0 residue**.

## Architecture round 02 — production tool runtime (2026-09-25)

### Implemented

- `harness.tools` now owns the complete provider-neutral typed catalog and
  `TypedToolRuntime`: filesystem reads (including `list` and bounded `image`),
  patch/edit/write/rename/delete/undo, seven read-only Git tools, synchronous
  and background process tools, stdin/output/kill, test/lint/typecheck/build,
  fetch/search, MCP, memory, question/todo/plan/task/finish. Every call is
  schema-validated before it reaches execution.
- `execution.workspace.SafeToolBackend` is the explicit policy/dispatch
  boundary. It supports `local_trusted`, `docker`, `native_os` (Linux
  bubblewrap when installed), and mandatory-Docker `verified_fix` profiles.
  A requested Docker/native profile never silently falls back to host code.
- `ToolPolicyEngine` evaluates tool, path, token-bounded command prefix,
  exact command arity, network domain (including redirect destinations), MCP
  server, side-effect class, agent mode, and actor. Deny wins over ask, ask wins
  over allow. `ApprovalStore` persists secret-free grants for session, project,
  or global scope; every grant remains bound to tool, exact redacted effect
  hash, context, and optional expiry. Approval decisions use the shared
  append-only redacted audit trail.
- Workspace safety now rejects protected reads as well as writes, relocates
  journal state out of the repository, rejects symlinked state, validates Git
  branch/revision identity, recognizes parent/child lease overlap, and fences
  the final replace/delete/rename/undo operation under the lease lock with a
  last-moment revision check. Existing-file backend writes require the model's
  expected revision; pre-existing dirty paths require explicit takeover.
- Added conflict-safe unified multi-file patch, atomic rename with reversible
  source/destination semantics, strict UTF-8 writes, special-file refusal, and
  hunk undo reconstruction from the target record plus later/undone agent
  records. A moved hunk or any unattributed post-image change conflicts rather
  than modifying the wrong location.
- Background processes have bounded incremental stdout/stderr, bidirectional
  stdin, kill/wait, a workspace-wide mutation lease, temporary-home cleanup,
  and Windows Job Object descendant cleanup. POSIX requests a real PTY;
  Windows honestly reports `pty_requested=true, pty_allocated=false` rather
  than claiming native ConPTY support.
- Local and Docker child environments delegate to `shared.security`; explicit
  container env additionally rejects URL-userinfo credentials. Docker rejects
  empty commands before daemon probing, token-owns cross-process build locks,
  does not mark a base image done when a peer never built it, and scrubs the
  environment of Docker CLI children.

### Final verification

- Required exact lane: `python -m pytest tests/test_workspace_security.py
  tests/test_sandbox.py tests/test_verify.py tests/test_verify_js.py
  tests/test_editor_prompts.py -q` -> **204 passed, 1 skipped**, exit 0, real
  Docker. The sole skip is Windows symlink creation privilege at
  `tests/test_editor_prompts.py:56`; it is blocked platform coverage, not a pass.
- Focused workspace security: **53 passed, 0 skipped**.
- Editor prompt/safety lane: **22 passed, 1 platform skip**.
- `python logs/dod/run_dod.py` -> **15/15 checks passed** on real Docker.
- `python -m execution.sandbox_adversarial` -> **24 attacks: 23 held, 1
  confirmed-by-design, 0 findings**; final report
  `logs/sandbox-adversarial/20260925-080848/adversarial_sequential.json`.
- Owned Ruff check/format, Python compile, and unscoped `git diff --check` pass.
  `graphify update .` rebuilt **22,579 nodes, 66,028 edges, 4,757
  communities**.
- `python -m pytest tests/test_agent_kernel.py -q -p no:randomly` -> **34
  passed, 2 failed** after the stricter revision precondition. Both failures are
  the same T1 integration gap described below; no T1-owned file was edited.

### Not yet implemented / blocked

- Terminal 1 must make the typed production runtime authoritative. Its
  `edit` ToolSpec still omits `expected_revision`, and its edit handler invokes
  `SafeToolBackend` without a read revision. Update both rather than capturing
  the current hash immediately before mutation. The public `run_agent` path
  also remains on the legacy adapter until Terminal 1 wires
  `TypedToolRuntime`/`ToolPolicyEngine` and removes backend-construction
  fallback.
- Native Windows PTY is unavailable with the current dependency set; local
  background processes still provide bounded bidirectional pipes. The native
  OS sandbox profile is implemented for Linux `bubblewrap` and fails closed on
  Windows rather than downgrading to trusted local execution.
- Process execution holds the workspace-wide lease and is approval-gated, but
  direct shell-created byte deltas are not converted into per-file/per-hunk undo
  records. Structured mutation tools have full conflict-safe undo; production
  callers should prefer them until a separate process-effect journal exists.
- No live model/provider or live web-search lane was run. No credential was
  inspected or retained. The default search path is SSRF-bounded and unit-tested
  with a deterministic fetcher, not counted as a live-provider pass.
- The accepted unquotaed host bind-mount disk-consumption exposure remains and
  is still reported by the adversarial driver as confirmed-by-design.

### Cross-terminal handoff

- **T1 / agent kernel:** in `harness/agent_kernel/tools.py`, make `read`
  return the execution revision and require `expected_revision` on edit/write/
  patch/delete/rename. In `harness/agent_kernel/strategy.py`, pass that model
  revision into `SafeToolBackend`; do not derive a fresh current revision after
  the model has already read. Route process calls through the backend profile,
  and remove the typed-local construction fallback. The machine-readable
  details are in `logs/architecture-round/terminal-02.json`.

## R2-02 — the flake gate is now capable of firing (2026-09-26)

**NEW `execution/flake_gate.py`. The gate was real in the vocabulary and
unreachable in the code; this round makes it expressible, testable and
measurable. It is NOT yet on the live path — the three call sites are the
handoff at the end of this section, and until they land the default config
still cannot fire the gate. That sentence is the honest state, not a caveat
about a finished thing.**

### The defect, arithmetically

`harness/config.py` shipped `baseline_reruns = 1`.
`execution/verify.py:428` computed
`run_count = 1 if not target_test else max(1, rerun_for_flake_check)`, so
`run_count == 1` for every configured value in `{0, 1}` (and for the default
`1`). `verify.py:441` then computed `flaky = len(set(outcomes)) > 1` over a
ONE-element list, which is unsatisfiable. `verify.py:357` documented this
correctly ("0/1 means a single run — **flaky can never be True**") and
nothing acted on it, so the documentation and the product disagreed and the
product lost: a test that passed once and failed on the third run was
reported as a clean, non-flaky pass.

One number was doing two jobs — "how many target runs" and "do we detect
flakes" — and the second job silently degraded to "no".

### What the module does

- **The split.** A **baseline** asks only "was this already broken before we
  touched anything?", which one run answers, and it is paid on every task, so
  `DEFAULT_BASELINE_REPETITIONS = 1` and it stays 1. Flake detection is a
  property of the **post-fix** run — the run that mints a completion claim —
  and `DEFAULT_POST_FIX_REPETITIONS = 2`, the smallest number of observations
  that can distinguish "stable" from "not stable". The cost argument for a
  cheap baseline is real, so it is not spent twice.
- **A three-valued verdict.** `flake_verdict(repetitions, outcomes)` returns
  `flake_check` in the closed set `{"flaky_detected", "not_flaky",
  "not_run"}`. `not_run` exists because `flaky=False` reads as "we checked and
  it was stable"; with one repetition that is a lie, so the one-run case says
  `not_run` and never `not_flaky`. `flaky` stays a plain bool with its
  historical value (no consumer changes behaviour), and
  `FlakeVerdict.detection_possible` is what a consumer must require before
  claiming stability was shown. `FLAKE_CHECK_VALUES` is exported so a
  consumer can reject an unknown value instead of defaulting to stable.
- **Fail-closed against its own caller.** The verdict is derived from the
  number of outcomes **observed**, never the number requested, so a caller
  that asks for 3 repetitions and reports 1 gets `not_run` rather than a
  manufactured "not_flaky". `FlakeVerdict.check()` returns the list of
  self-inconsistencies (empty when fine) so a caller can assert its own
  receipt instead of trusting a construction site it does not own.
- **Timeout stays a third outcome.** `pass`/`fail`/`timeout` are re-exported
  from `execution.result_parsing`, so there is one vocabulary in the tree.
  Timeout is decided first: a pass-then-hang mix is `flaky_detected` (not a
  stable pass), and a run that times out EVERY time is `not_flaky` with
  `timed_out=True` — consistently broken, not intermittently broken.
- **Nothing can raise out of the gate.** `observe_repetitions` records a
  repetition whose runner raised as `OUTCOME_ERROR` and continues, because
  one broken repetition is itself evidence and raising would discard the
  repetitions that already ran. `render_receipt` never raises. Only
  `resolve_repetitions`/`repetitions_for_stage` raise, and only on a
  configuration bug (a `bool`, a non-int, an unknown stage) that should be
  loud.
- **Cost is bounded, not just documented.** `MAX_REPETITIONS = 10` is a hard
  ceiling; a mistyped config value is clamped DOWN with the clamp recorded in
  `source`/`notes` (never silently), and a caller may tighten it further. A
  `bool` is refused rather than coerced, because `int(True) == 1` would
  silently disable the gate on a typo.
- **Config lookup is by key MEANING, not truthiness.** An absent key takes
  the stage default; an explicit `0` is a deliberate "one run, no detection".
  A truthiness check would collapse those two cases and silently re-enable
  detection a caller had switched off. `REPETITION_CONFIG_KEYS` maps
  `baseline -> baseline_reruns` and `post_fix -> post_fix_reruns`, and the
  post-fix stage falls back to the legacy `baseline_reruns` key so a config
  written before the split still resolves to ONE number rather than silently
  to a different one.
- **Auditability.** `FlakeVerdict.to_dict()` / `FlakeRun.to_dict()` carry
  `repetitions`, `requested_repetitions`, `observed_outcomes`,
  `distinct_outcomes`, `timed_out`, `detection_possible`, the notes, and the
  MEASURED `elapsed_s` / `per_repetition_s`. The receipt is
  JSON-serializable, so the claim is reconstructable from a trace row without
  re-running anything. `FlakeRun.last_result` is still the FINAL repetition,
  so switching a call site to this module cannot change which run decides
  `target_test_passed`.

### The public surface (import from `execution.flake_gate`)

```
constants   OUTCOME_{PASS,FAIL,TIMEOUT,NO_TESTS,ERROR}   # re-exported
            TIMEOUT_EXIT_CODES, FLAKE_DETECTED, NOT_FLAKY, NOT_RUN,
            FLAKE_CHECK_VALUES, MIN_REPETITIONS_FOR_DETECTION,
            DEFAULT_POST_FIX_REPETITIONS, DEFAULT_BASELINE_REPETITIONS,
            MAX_REPETITIONS, STAGE_BASELINE, STAGE_POST_FIX,
            REPETITION_CONFIG_KEYS
types       FlakeVerdict, FlakeObservation, FlakeRun, RepetitionResolution
functions   outcome_label(exit_code, timed_out) -> str
            classify_run(result, *, expected_tests=None) -> str
            flake_verdict(repetitions, outcomes) -> FlakeVerdict
            observe_repetitions(run_once, repetitions, *, label=None,
                                expected_tests=None) -> FlakeObservation
            verdict_for(observation) -> FlakeVerdict
            evaluate_repetitions(run_once, repetitions, *, label=None,
                                 expected_tests=None) -> FlakeRun
            resolve_repetitions(value, *, stage, default, ceiling)
            repetitions_for_stage(stage, config, *, ceiling=None)
            attach_evidence(result, verdict) -> result
            evidence_of(result) -> Optional[FlakeVerdict]
            render_receipt(verdict) -> str
```

### MEASURED cost (real Docker lane, not estimated)

`execution.sandbox.execute_sandboxed`, 3 samples per repetition count, warm
per-repo image, trivial 2-test fixture, target command
`python -m pytest tests/test_target.py -q -p no:randomly -o addopts=`:

| repetitions | median wall | per-repetition |
|---|---|---|
| 1 | **3.36s** | 3.36s |
| 2 (`DEFAULT_POST_FIX_REPETITIONS`) | **6.41s** | 3.20s |
| 3 | **7.59s** | 2.53s |

So the default 1 -> 2 change costs **+3.05s median, +91% of the target-run
phase**, and the marginal repetition is CHEAPER than the first because the
image and page cache are already warm (i.e. the cost is dominated by the
fixed per-call container overhead, not by the test). For a real target test
the marginal cost is the test's own runtime, so scale by that, not by 3s.

**The per-task multiplier matters more than the absolute number.**
`harness/core.py` verifies the target after every step turn, so a 15-turn task
pays the extra run up to 15 times. Hence the recommended split below. Do NOT
blindly raise every call site to 2 and call the cost "3 seconds".

### What is built, what is stubbed, what is NOT implemented

- **Built and tested:** the whole module above. 41 tests, four of them the
  required proofs, plus the non-vacuity controls.
- **Deliberately not built:** a live call site (ownership — see the handoff),
  and any change to `shared/types.py` (another owner's file), so the
  repetitions/outcomes are recorded as ADDITIVE INSTANCE ATTRIBUTES on the
  `VerificationResult` rather than as new dataclass fields. If the integrator
  prefers real fields, that is a `shared/types.py` change with a
  `field(default=...)`, not an `execution/` one.
- **NOT implemented:** per-repetition environment isolation (repetitions run
  against the same repo copy and the same container shape, so an
  order-dependent test can still mask itself across repetitions — a genuinely
  independent repeat would need a fresh copy per repetition, which costs a
  tree copy per run and is deliberately not paid by default); a
  statistical flake RATE (the verdict is the documented boolean
  "any difference", not a rate with confidence); and any Docker lane inside
  the test suite (the required proofs use the documented HOST lane
  `execution.flake.run_local_command`, which is deliberately NOT wired into
  `verify()` — production verification still goes through Docker only).

### Verification actually run (this tree, 2026-09-26)

- `python -m pytest tests/test_ceiling_r2_02_flake.py -q -p no:randomly` ->
  **41 passed**. The four required proofs, none of them vacuous:
  - `test_default_configuration_can_detect_a_genuinely_flaky_test` — a real
    alternating pytest fixture (counter file: pass, fail, pass, ...) run
    through REAL `python -m pytest` processes reaches `flaky_detected` at the
    DEFAULT repetition count, and asserts `repetitions == policy.repetitions
    >= 2` plus the exact `observed_outcomes` tuple.
  - `test_stable_test_reports_not_flaky_with_at_least_two_repetitions` — a
    real stable fixture over 3 repetitions asserts `repetitions == 3 >= 2`,
    `detection_possible is True` and the identical outcome vector. The
    assertion is on the NUMBER, not only the boolean, so collapsing the series
    to one run cannot leave it passing.
  - `test_the_same_stable_test_on_one_repetition_reports_not_run` — the
    control: same fixture, same runner, one repetition, `not_run`. This is
    what makes the previous test's `not_flaky` mean something.
  - `test_a_consistently_hanging_test_is_consistently_broken_not_flaky` — a
    real `sleep 30` exceeding a real 2s timeout budget through the real local
    runner, not a synthesised label.
  - plus the exhaustive 2-repetition label matrix, the "requested 3 / observed
    1" fail-closed case, the raising-repetition case, the ceiling/bool/key-
    meaning policy cases, the JSON-on-disk round trip, and
    `evidence_of(legacy) is None` for a result with no receipt.
- `python -m pytest tests/test_verify.py -q -p no:randomly` -> **28 passed**
  against the real Docker daemon (the module's own suite; this round is purely
  additive so nothing in it moved).
- `python -m pytest tests/test_stubs_and_deps.py -q -p no:randomly` ->
  **10 passed** (the Boundary-1/stub parity lane).
- `python -m evals.run --check` -> **14/14 CLEAN**.
- `ruff check` + `ruff format --check` clean on both new files; `compileall`
  clean; scoped `git diff --check` clean.
- Cost measurement driver: `Temp/opencode/r2_02_flake_cost.py` (NOT in the
  suite), report written beside its fixture repo.
- **No live-provider lane was run** and none is claimed. No credential was
  inspected, printed, or retained.

### R2-02 cross-terminal requests — the three call sites (for R2-01 / the integrator)

All three files were owned by another terminal in the same parallel group, so
these are written as instructions, not applied edits. Each block is the
replacement for the `rerun_for_flake_check=int(cfg.get("baseline_reruns", 1))`
line and, where noted, the receipt fields beside it.

**Shared import (all three files):**
`from execution.flake_gate import attach_evidence, evaluate_repetitions, repetitions_for_stage, render_receipt, STAGE_POST_FIX`

**(1) `execution/verify.py` — the repetition loop, around line 428.**

Replace the manual loop:
```python
outcomes: List[str] = []
run_count = 1 if not target_test else max(1, rerun_for_flake_check)
for _ in range(run_count):
    res = execute_sandboxed(repo_path, target_cmd, verify_timeout_s, allow_network=allow_network)
    ...
flaky = len(set(outcomes)) > 1
```
with:
```python
def _run_target_once(_index: int):
    return execute_sandboxed(repo_path, target_cmd, verify_timeout_s, allow_network=allow_network)

run_count = 1 if not target_test else max(1, rerun_for_flake_check)
raw: List[str] = []
def _capture(res) -> None:
    raw.append(_format_run(target_cmd, res))

flake_run = evaluate_repetitions(
    _run_target_once, run_count, expected_tests=1 if target_test else None
)
for res in flake_run.observation.results:
    if res is not None:
        _capture(res)
outcomes = list(flake_run.verdict.observed_outcomes)
flaky = flake_run.verdict.flaky
```
and build the result with `attach_evidence(VerificationResult(...), flake_run.verdict)`.
`classify_run` reproduces the CURRENT labelling exactly (timeout first, then
the report-first parser with `expected_tests=1`), so this is a refactor, not a
semantic change. Keep `target_passed = outcomes[-1] == "pass"` — it is the same
"last run decides" rule. **Note the `raw` transcript:** `evaluate_repetitions`
keeps the raw results in `observation.results`, so `_format_run` can still be
emitted per repetition; `execution.rationale.build_rationale` reads
`data.raw` from the `baseline_verify` trace event, so do NOT drop it.
Optionally append a `reports`-shaped row, or a new `flake` sink, carrying
`flake_run.to_dict()` so the repetitions/outcomes reach the trace.

**(2) `harness/core.py` — the `baseline_reruns` sites at 1459, 2140, 2712.**
They are all POST-FIX, which is the conflation being fixed; none of them is a
baseline. Two of them GATE, one does not:

- **1459 (final gate) — GATING. Use 2.** This is the run that mints the
  completion claim, so this is where a flaky test must be caught.
  ```python
  post_fix = repetitions_for_stage(STAGE_POST_FIX, cfg)
  final_v = verify(str(paths.work), cfg.get("target_test"),
                   rerun_for_flake_check=post_fix.repetitions, ...)
  ```
- **2140 (agent-written tests, post-fix) — GATING. Use 2.** A post-fix failure
  poisons the attempt, so it has the same obligation as the final gate.
- **2712 (per-step SUBMIT-time checkpoint) — NOT gating. Use 1.** Its `flaky`
  value gates nothing: the step just ends (`return True, "checkpoint passed",
  v`) and success is minted only at 1459. Running the target 2x after every
  step turn is up to 15 extra runs per task for a verdict nobody reads. Pass
  `rerun_for_flake_check=1` and the receipt will honestly read
  `flake_check="not_run"` — which is correct, and is the point of the third
  value. If R2-01 prefers uniformity over cost, use 2 here too and expect the
  15x multiplier.
- Add `repetitions` / `observed_outcomes` / `flake_check` to the
  `final_verify`, `agent_tests_verify` and `verify` trace rows so the claim is
  on disk.
- The three BASELINE sites (`core.py:511`, `core.py:2092`,
  `build_mode.py:276`, `build_plan.py:932`, `runtime/orchestration.py:1495`)
  currently pass the literal `0`. Switching them to
  `repetitions_for_stage(STAGE_BASELINE, cfg).repetitions` is
  **behaviour-preserving** (0 and 1 both mean one run) and is the honest way
  to express "a baseline costs one run".
- `harness/agent_kernel/completion.py:97` has the same
  `baseline_reruns` read and the same decision to make.

**(3) `harness/agent_loop.py:2520`** — `_run_verify` is the general agent's
one-shot declared-test run. It is not a completion gate (the agent loop has
no verifier gate by default), so **1 repetition is defensible**; use 2 if the
integrator wants the loop's `flaky` to be a real signal. Either way, add
`"flake_check": <verdict>, "repetitions": <n>, "observed_outcomes": <list>` to
the `out` dict AND to the `emit("verify", {...})` payload, because
`agent_loop.py:1560` and `cli/tracelog.py:1094-1131` read `flaky` from those
rows and would otherwise render an unchecked run as a stable one.

**(4) `harness/config.py` — three DEFAULTS changes (all additive):**
```python
"baseline_reruns": 1,      # UNCHANGED value; its MEANING is now the
                           # baseline count (one run), not the post-fix count
"post_fix_reruns": 2,      # NEW: post-fix/final-gate repetitions; >=2 so the
                           # flake gate can actually fire
"flake_repetitions_cap": 10,  # NEW: hard ceiling on repetitions
```
**Read this before adding them:** `DEFAULTS` is merged into EVERY task and
every eval arm, so `post_fix_reruns: 2` is a behaviour-changing default that
silently doubles the target-run phase of every run and every eval arm. It is
the change this round is FOR (a gate that cannot fire is not a gate), and it
is why the cost table above exists. Two consequences the integrator must
decide, not discover later:
- the eval arms in `evals/tasks.py` / `evals/daily_driver.py` (Terminal 10's
  files) will pay the extra run. Either pin `"post_fix_reruns": 1` there to
  keep the eval timings comparable, or accept the change and re-baseline the
  arm timings — but do NOT leave it implicit;
- `evals/daily_driver.py:4702/4711` and `evals/run.py:725` read
  `result.verification.flaky` for the `no_false_verified_success` guard. That
  guard still works (`flaky` keeps its meaning and the 3-valued verdict only
  ADDS `not_run`), but a consumer that wants to assert stability was shown
  should read `detection_possible` / `flake_check` instead of `flaky`.
- `harness/config.py:41`'s comment (`# reruns for flake detection in
  verify()`) is what caused the conflation. It should say the key is the
  BASELINE count, and point at `post_fix_reruns` for the gate.

**(5) Optional, if the integrator prefers real dataclass fields over
attributes:** add to `shared/types.py` (NOT an `execution/` file)
`flake_check: str = "not_run"`, `repetitions: int = 0`,
`observed_outcomes: List[str] = field(default_factory=list)` with
`field(default=...)`, so every existing constructor call stays valid, and have
`attach_evidence` set fields instead of attributes. `runtime/serialize.py`
would then need the same round-trip treatment the existing
`structured_feedback` field got. Until that happens the instance-attribute
route is what `attach_evidence` uses, and `evidence_of(result) is None` is how
a consumer detects a legacy single-run result.

**What is NOT a pass until (1)-(4) land:** the flake gate firing on the
default configuration. This round built and measured the mechanism; it did not
put it on the live path, and no test in this round pretends otherwise. The
self-arming pin
`tests/test_ceiling_r2_02_flake.py::test_verify_is_either_unwired_or_uses_a_fireable_repetition_default`
reads `execution/verify.py`'s source and asserts the fireable default either
way, so it becomes an ACTIVE check the moment the wiring lands rather than
turning into a rubber stamp.

---

## VEX-CEILING Round 2 / R2-05 - baseline failure set and environment triage (2026-09-26)

**Closes R2-G05, R2-G06, R2-G24. One new module (`execution/baseline_set.py`)
and one EXTENDED existing classifier (`harness/tool_errors.py`). The two
contested call sites are NOT applied - they are filed below as written
requests, because `execution/verify.py` is R2-01's and `harness/core.py` is
R2-04's during their slots.**

The defect: the verifier answered "did the target pass" and never "was this
test already broken before I touched anything", and it treated every failure as
a code failure. Both halves let a run mislead: a repository that arrived red
reads exactly like one the agent broke, and a broken MACHINE is handed to the
model as a defect to fix, so the loop spends its budget editing a working tree
that was never broken.

### 1. What is built

| surface | what it owns |
|---|---|
| `execution/baseline_set.py` (NEW) | the recorded pre-existing failure set, the honest triple, environment triage, the run end state, persistence |
| `harness/tool_errors.py` (EXTENDED, appended section) | the environment-vs-CODE classification: five named classes in the EXISTING `POLICY` table |

**The honest triple.** `classify_run(result, baseline=..., target_test=...) ->
BaselineVerdict` returns `target_passed`, `new_failures` (post-fix failures the
baseline did NOT already contain - the only ones this run is responsible for)
and `preexisting_failures` (the baseline's recorded set, named and counted
whether or not it still fails). `preexisting_still_failing` is the INTERSECTION
(observed post-fix AND already known at baseline) - that is what makes
"pre-existing, not a regression" a checkable claim instead of an assertion.
`summary_line()` renders the triple in one quotable sentence.

**`blocks_success` is fail-closed, and that is pinned.** It is True whenever
the verifier's own evidence is not green, when a new failure appeared, when zero
tests were collected, and when the failure is environment-class. A test walks
the whole truth table and asserts the new gate refuses EVERY combination the
existing mint (`target and regression and not flaky`) refuses, and refuses
exactly ONE extra (the vacuous green). This module can only add a reason to
refuse a claim.

**The five environment classes** are `env_docker_unavailable`,
`env_missing_interpreter`, `env_unreachable_network`, `env_missing_dependency`,
`env_repo_permission` - the closed, ordered `ENVIRONMENT_KINDS` tuple. They are
ROWS in the existing `POLICY` table, not a second table, and each has a distinct
action slug (`report_*`). What distinguishes them is
`is_repairable_by_edit() is False` and `RecoveryPolicy.on_tool_error` returning
`stop=True` with `next_command=None`: the honest next action after "the machine
is wrong" is to stop, and inventing a narrower command is how a loop talks
itself into a repair loop.

**The classification is delegated, not forked.** `execution/baseline_set.py`
calls `harness.tool_errors.classify_environment` /
`environment_from_result` / `on_environment_failure` /
`render_environment_report`; it contains no pattern table of its own (pinned by
a test that reads the source for `_NETWORK_RE`).

**The end state.** `should_stop_for_environment(verdict)` returns `None` for
every run that may continue, and otherwise a dict with
`status="error"` (the honest member of `TaskResult.status`'s closed Literal),
`classification`, `action`, `operator_action`, `repairable_by_edit=False` and a
human `report()`. The payload contains no success/verified word, and a caller
that IGNORED the dict still could not mint success, because `blocks_success` is
independently True for the same evidence: the stop is belt, the gate is braces.

**Persistence.** `record_baseline(...)` writes
`logs/{task_id}/baseline_set.json` (`schema_version: 1`, tmp+replace). A missing,
corrupt, truncated, or future-version record loads as `None` and every
post-fix failure is then reported as NEW. A write that cannot land is a NOTE,
never a failed verification.

### 2. Three rules that are load-bearing, not decoration

- **An unobserved baseline is not an empty baseline.** A pristine run that timed
  out, crashed, or collected nothing yields `baseline_observed=False` and an
  empty set that means UNKNOWN. `classify_run` then reports every post-fix
  failure as new. Excusing failures against evidence that was never collected is
  how a real regression becomes invisible.
- **An UNDECLARED import that fails is a code defect.** Only a module the
  repository DECLARES (read from `requirements*.txt`, `pyproject.toml`,
  `setup.cfg`, `package.json` by `declared_dependencies`) may be attributed to
  the environment. Everything else stays the existing `import_error` and the run
  keeps repairing - otherwise every genuine bug that imports something it never
  declared would be excused and the run stopped.
- **A harness POLICY refusal is never an environment fault.** The deny guard's
  `PermissionError` shares the OS wording; without an explicit exclusion,
  `env_repo_permission` would report "fix the machine" for a refusal the harness
  made on purpose. `_HARNESS_GUARD_RE` excludes it first, and a test pins that.

### 3. Bugs this found in its OWN first cut (kept, because the next terminal
would otherwise re-introduce them)

- Parsing a PASSING capture through `execution.feedback.to_objects` produced a
  fake failure: that helper deliberately returns a catch-all object when it
  cannot parse a run, which is right for a FAILING run and wrong for a green
  one. `baseline_set_from_verification` now extracts nothing when the outcome is
  `pass`. Without this, "4 passed" would have been reported as one failure.
- A `collection_error` object on a zero-test run was being recorded as a failing
  TEST. Nothing was collected, so there is no per-test set; it is carried by
  `outcome=no_tests` plus a note instead.
- An `error` outcome with no counts is a BROKEN run, not a zero-test run.
  `execution.result_parsing` keeps those distinct on purpose and so does this
  module; collapsing them would invent a "0 tests collected" claim.
- `_module_root("zope.interface")` is `zope`, and the manifest name is
  `zope.interface`. A naive comparison misses every renamed distribution, so
  `_normalize_dependencies` splits on `-`/`_`/`.` and keeps every part.

### 4. What is STUBBED / mocked, and why

Nothing in the production path is stubbed. The honest limits of the evidence:

- **No real absent-Docker-daemon run.** The class is driven with the exact
  exception class name the sandbox raises (`SandboxUnavailableError`, matched by
  NAME so this module keeps its zero-boundary-dependency property) and with the
  daemon's own message. That is strong evidence for the classifier and is NOT a
  measurement of a real stopped daemon.
- **`declared_dependencies` degrades without a TOML parser.** `tomllib` (3.11+)
  then `tomli`, then a bounded regex over `dependencies = [ ... ]`, and the
  degraded read is reported in `notes` rather than presented as authoritative.
  This host is Python 3.10, so the third path is the one exercised here.
- **This suite is HOST-ONLY.** No Docker, no model, no network. The captures
  are pytest-shaped text in `execution.verify._format_run`'s own `$ cmd` /
  `exit=N` framing, so the parse path is the real one, but nothing here is
  evidence about the real Docker verifier or a real provider.

### 5. What is NOT implemented (do not assume it is)

- **Neither call site is wired.** `execution/verify.py` and `harness/core.py`
  contain no reference to `baseline_set` (pinned by a test that reads both
  sources). Until one of the requests below is applied, the baseline set is
  computed only by tests and is INERT in a real run. This is a filed handoff,
  not a claim of coverage.
- **No per-test PASS set.** Only failures are recorded, so "this test passed
  before and passes now" is not representable and `preexisting_still_failing`
  is an intersection of FAILURES. A test that was skipped at baseline and fails
  after is correctly reported as new; one that was absent at baseline and fails
  after is also new. Neither is a false "pre-existing".
- **Not wired into `harness/agent_kernel/` (the daily path).** The kernel has
  its own completion policy and does not call `verify()` on every step; a
  kernel run gets no baseline set from this round.
- **No JUnit/JSON report ingestion here.** `baseline_set_from_verification`
  accepts a `TestRunReport` and uses it as authoritative, but `verify()` does
  not currently produce one for the caller to pass (the `reports` sink collects
  report DICTS, not the object). R2-01's `reports` plumbing is the seam.
- **No model-facing prompt change.** The model still sees `structured_feedback`
  alone; telling it "this failure is pre-existing, do not fix it" is a
  `harness/prompts.py` decision this prompt does not own.

### 6. Cross-terminal requests (apply these, or the feature is inert)

**A. `execution/verify.py` result assembly - R2-01 (you own this file).**
`verify()` is stateless and does not know whether it is evaluating the pristine
or the edited tree, so give it the phase, not a guess:

```python
# new keyword-only params on verify(), all default-safe (verify() has no
# config parameter today, so this is the FIRST way it can see the keys - the
# harness passes its resolved `cfg` at both call sites):
#   run_dir: str = ""                    # logs/{task_id}/ the record lives beside
#   phase: Optional[str] = None          # "baseline" | "postfix" | None (neither)
#   config: Optional[dict] = None        # the resolved Task.config
```
Then in `verify()`, right before the existing `return _attach_structured_feedback(...)`:

```python
from execution.baseline_set import classify_run, record_baseline
cfg = config or {}
_verdict = None
if run_dir and "baseline_set_enabled" in cfg and phase == "baseline":
    _baseline = record_baseline(
        result, run_dir=run_dir, target_test=target_test,
        repo_path=repo_path, config=cfg,
    )
    reports.append({
        "outcome": "info", "source": "baseline",
        "confidence": "high", "exit_code": None, "timed_out": False,
        "tests_collected": _baseline.collected, "tests_passed": _baseline.passed,
        "tests_failed": _baseline.failed, "tests_skipped": _baseline.skipped,
        "passed": None,
        "notes": [f"baseline: {_baseline.outcome} observed="
                  f"{_baseline.baseline_observed} preexisting={_baseline.preexisting_count}"],
    })
elif run_dir and "baseline_set_enabled" in cfg and phase == "postfix":
    _verdict = classify_run(
        result, run_dir=run_dir, target_test=target_test,
        repo_path=repo_path, config=cfg,
    )
    reports.append({
        "outcome": "info", "source": "baseline_verdict",
        "confidence": "high", "exit_code": None, "timed_out": False,
        "tests_collected": None, "tests_passed": None,
        "tests_failed": _verdict.new_failure_count, "tests_skipped": None,
        "passed": None,
        "notes": [_verdict.summary_line(), "zero_tests="
                  f"{_verdict.zero_tests_collected}", "environment="
                  f"{_verdict.environment.classification}",
                  "blocks_success=%s" % _verdict.blocks_success],
    })
```
Gate BOTH branches on the key being PRESENT (`"baseline_set_enabled" in cfg`),
never on a value in `DEFAULTS` - a value there is merged into every task and
every eval arm at once. The snippet above is not pseudocode: it was rehearsed
end-to-end against this module (pristine record written, post-fix triple
reported, the env-fault stop returning `status="error"`, and the absent-key case
writing nothing) before being filed here.
`VerificationResult` itself is unchanged; the receipt rides the
existing `reports` sink.

**B. `harness/core.py` verification region - R2-01 / R2-04 / the integrator.**
Three edits, all additive:

1. Baseline region (today `core.py:505-533`, after `base_v` is built):
```python
baseline_set = None
if "baseline_set_enabled" in cfg:
    from execution.baseline_set import record_baseline
    baseline_set = record_baseline(
        base_v, run_dir=str(paths.log_dir), target_test=cfg.get("target_test"),
        repo_path=str(paths.pristine), config=cfg,
    )
    trace.log("baseline_failure_set", {
        "outcome": baseline_set.outcome,
        "observed": baseline_set.baseline_observed,
        "preexisting": baseline_set.preexisting_count,
        "preexisting_ids": list(baseline_set.failing_ids)[:50],
        "environment": baseline_set.environment.classification,
    })
```
2. Final gate (today `core.py:1480-1491`, immediately after
`final_v = _with_baseline(...)`) - this is where the run must STOP on a
non-repairable environment fault instead of burning attempts:
```python
from execution.baseline_set import classify_run, should_stop_for_environment
_verdict = classify_run(
    final_v, run_dir=str(paths.log_dir), target_test=cfg.get("target_test"),
    repo_path=str(paths.work), config=cfg,
)
trace.log("baseline_verdict", _verdict.to_dict())
_env_end = should_stop_for_environment(_verdict)
if _env_end is not None:
    trace.log("task_end", {"status": _env_end["status"], "reason": _env_end["reason"]})
    return _result(task, _env_end["status"], attempts, None, last_verify, model,
                   trace, note=_env_end["reason"])
```
   The same `_verdict` should then feed the failure branch (today
   `core.py:1655-1672`) so the model is told which failures are INHERITED
   rather than handed an inherited failure as a defect to repair.
3. The "target already passes on pristine" early return (today
   `core.py:535-556`): the target passing on a repository whose suite is red
   should carry the pre-existing count into `state.json`/the trace.

**C. `harness/config.py` keys - R2-04 owns it during its slot.** Do NOT add
`baseline_set_enabled` / `environment_triage_enabled` to `DEFAULTS`: they are
key-presence opt-ins, and a default there switches every task and every eval arm
in the same commit. `baseline_set_max_failures` (200) and
`baseline_set_max_summary_chars` (300) are pure caps and are safe in `DEFAULTS`.
A test currently asserts none of the four are in `DEFAULTS`; that assertion is
the guard and must be updated in the same change that adds them.

**D. `INTERFACES.md` Change Log** - not edited by this prompt (R2-01 owns the
file in this group). Paste-ready entry:

> - 2026-09-26 (Ceiling Round 2 / R2-05 - baseline failure set and environment
>   triage): **NEW `execution/baseline_set.py`; `harness/tool_errors.py` gained
>   five environment rows in its EXISTING `POLICY` table plus
>   `ENVIRONMENT_KINDS` / `is_environment_kind` / `is_repairable_by_edit` /
>   `environment_action` / `classify_environment` / `environment_from_result` /
>   `on_environment_failure` / `render_environment_report`. No Boundary 0-5
>   signature, event kind, or serialized field was removed or renamed, and
>   `shared/types.py` was NOT edited: `VerificationResult` is unchanged and the
>   success mint is untouched.**
>   (1) `execution/baseline_set.py` - `BaselineSet` (the recorded pre-existing
>   failure set plus `baseline_observed`), `BaselineVerdict` (the honest triple
>   `target_passed` / `new_failures` / `preexisting_failures`, plus the
>   `preexisting_still_failing` intersection, `zero_tests_collected` and a
>   fail-closed `blocks_success`), `classify_run`, `EnvironmentVerdict`,
>   `triage_environment`, `should_stop_for_environment` (the run end state:
>   `status="error"`, `repairable_by_edit=False`), and the atomic
>   `logs/{task_id}/baseline_set.json` record (`schema_version: 1`, an unknown
>   version is refused).
>   (2) `harness/tool_errors.py` - the environment question is answered in the
>   ONE classifier: the five classes are `POLICY` rows with distinct action
>   slugs, `is_repairable_by_edit()` is False for each, and
>   `RecoveryPolicy.on_tool_error` returns `stop=True` with no next command. An
>   UNDECLARED import that fails stays `import_error` (a code defect); a harness
>   deny-guard refusal is never an environment fault; an absent daemon is matched
>   by the exception's CLASS NAME, so the module keeps its zero-boundary
>   dependency property.
>   (3) NOT WIRED, recorded as a handoff: `execution/verify.py` and
>   `harness/core.py` contain no reference to `baseline_set`
>   (`tests/test_ceiling_r2_05_baseline.py::test_nothing_in_this_round_touched_the_verifier_or_the_run_loop`
>   reads both sources and pins it). The exact call sites are in
>   `execution/AGENTS.md` ("Cross-terminal requests").
>   Verified: `python -m pytest tests/test_ceiling_r2_05_baseline.py -q
>   -p no:randomly` -> **31 passed** (host-only: no Docker, no model, no
>   network). `tests/test_ceiling_r2_05_baseline.py tests/test_tool_errors.py
>   tests/test_recovery_steering.py` -> **124 passed**. Plus
>   `tests/test_stubs_and_deps.py tests/test_config_trace_state.py` -> **148
>   passed**, `tests/test_ceiling08_verification.py tests/test_feedback.py` ->
>   **103 passed** (real Docker), `tests/test_agent_loop.py
>   tests/test_e2e_run_task.py` -> **90 passed** (real Docker).
>   `python -m evals.run --check` -> **14/14 CLEAN**. Owned `ruff check` clean;
>   `ruff format` applied to the two NEW files ONLY (`harness/tool_errors.py` is
>   a shared file and was not reformatted whole-file, per the shared-file
>   protocol); `compileall` clean. **No live-provider lane and no
>   real-stopped-daemon lane were run, and neither is claimed.**

**E. `harness/AGENTS.md`** - this prompt edited `harness/tool_errors.py` but
that file's module doc is T1's and was NOT edited (shared-file protocol). The
paragraph describing the appended environment section is section 4 of
`harness/tool_errors.py`'s own module docstring; please copy it into
`harness/AGENTS.md` when you next own that file, so the two do not drift.

### 7. Verification actually run (this tree, 2026-09-26)

- `python -m pytest tests/test_ceiling_r2_05_baseline.py -q -p no:randomly` ->
  **31 passed** in 0.70s. All four REQUIRED proofs are present and named:
  `test_a_target_already_failing_pre_fix_is_preexisting_and_the_run_says_so`,
  `test_a_newly_failing_test_is_a_regression_and_blocks_success`,
  `test_a_missing_declared_dependency_is_environment_and_ends_non_repairable`,
  `test_a_zero_collected_run_is_never_a_pass`.
- `tests/test_ceiling_r2_05_baseline.py tests/test_tool_errors.py
  tests/test_recovery_steering.py` -> **124 passed** (the two suites that own
  the extended classifier, unmodified).
- `tests/test_stubs_and_deps.py tests/test_config_trace_state.py` -> **148
  passed** including the above.
- `tests/test_ceiling08_verification.py tests/test_feedback.py` -> **103 passed**
  in 148s, including that round's **4 real-Docker** proofs. This is the
  selection that owns `execution/result_parsing.py` and `execution/feedback.py`,
  the two parsers this module consumes; it did not regress.
- `tests/test_agent_loop.py tests/test_e2e_run_task.py` -> **90 passed** in
  466s against the **real Docker daemon** (28 e2e). `harness/agent_loop.py` and
  `harness/core.py` are the callers of `RecoveryPolicy.on_tool_error`, so this is
  the lane that would have caught a broken environment branch.
- `python -m evals.run --check` -> **prompt task set: 14/14 ok, verdict CLEAN**.
- `python -m ruff check` on all three owned files -> **All checks passed**.
  `python -m ruff format` on the two NEW files only. `python -m compileall -q`
  on all three -> clean.
- **Not run:** no live-provider lane (no credential was inspected or retained);
  no real stopped-Docker-daemon lane; no full `python -m evals.run` matrix
  (only `--check`); no `tests/test_verify.py` / `test_sandbox.py` Docker
  selection - none of them is in this module's call path, and
  `execution/verify.py` was not edited. Those are BLOCKED / NOT SELECTED, not
  passes.

## R2-01 - the verification-intelligence layer is wired into production (2026-09-26)

**One new module, one delegation point in `verify.py`.** The dark subgraph
(`verification_intelligence`, `spec_ledger`, `independent_evidence`) had zero
production inbound edges. It has one now, and it is opt-in per run.

### The seam, and the two details that make it safe

    execution.verify.verify(..., intelligence_config=None)      # unchanged for every existing call
        -> any(key in intelligence_config for key in _INTELLIGENCE_CONFIG_KEYS)
             -> execution.verification_gate.run_intelligent_verify(...)
        -> otherwise: the pre-existing body, BYTE-IDENTICALLY

Three things about that shape are deliberate and are each pinned by a test:

- **The key tuple lives in `verify.py`, not in the pipeline.** Exactly like the
  Ceiling-14 seam: `runtime/model_router.py` owns `_RESILIENCE_CONFIG_KEYS` and
  lazily imports `runtime/provider_gateway`. That is not a style preference. My
  FIRST version put the test in `verification_gate`, and the very first test run
  found the consequence: when the pipeline failed to import, the `except` branch
  needed a function it could not have imported, and an *unconfigured* caller
  would have been served a refusal because the module was broken. A seam that
  cannot answer "did you ask for me?" without loading the thing it gates is not
  a seam. `verification_gate.INTELLIGENCE_CONFIG_KEYS` re-exports the tuple, so
  there is still exactly one literal.
- **Activation is key-PRESENCE, and nothing is in `DEFAULTS`.** All 15 keys are
  opt-in on presence; `{"verification_require_spec": False}` still enters the
  pipeline, because the operator wrote the key and turned that one gate off
  inside it. `tests/test_verification_gate_wiring.py::test_no_intelligence_key_is_in_the_harness_defaults`
  fails if anyone adds one of them to `harness/config.py::DEFAULTS` - a default
  is merged into every task, so it would switch every task and every eval arm at
  once and "absent" would stop meaning *unchanged behaviour*.
- **A pipeline that cannot be imported is a refusal, not a fallback.** An
  opted-in caller is never quietly served the weaker baseline; it gets a refusing
  `VerificationResult` and an `intelligence_unavailable` receipt row naming the
  reason.

### The rungs

`RUNGS = ("baseline", "spec", "independent")`, and every `GateVerdict` names the
one that produced it in three places: a `reports` row (`rung`/`gate`/`mandatory`
beside the existing `outcome`/`source`/`confidence`/`notes` keys), a
`## verification-gate` block appended to `raw_output`, and one
`verification_gate` unified-trace event. `grep '  rung=' logs/<task>/trace.jsonl`
answers "which mechanism claimed this was verified" without a parser.

- `baseline` - `target_test_passed and regression_passed and not flaky`, i.e. the
  harness's own mint condition, recorded and never applied. It cannot disagree
  with the mint because it reads the same booleans.
- `spec` - two gates. `spec_intact` is the seal diff-guard; `spec_obligations`
  RE-RUNS each sealed item's declared test references as its own sandboxed pytest
  selection and requires each to be collected AND passing. Running the named
  tests is the point: the `passes` flag is the agent's own report and the seal
  deliberately excludes it. It is also what makes a renamed or deleted test
  unable to satisfy its own obligation.
- `independent` - held-out acceptance plus the judge, opt-in per run. The
  judge's verdict can only REFUSE. There is no code path that sets a boolean
  back to `True`; that is the whole reason it is safe to put in front of a
  fail-closed mint.

### The ledger degrades loudly, in four shapes

Missing, unparseable, unsealed, and required-but-absent each produce a FAILING
gate with the reason plus a `degraded:` note. A *tampered* spec is deliberately
NOT filed as a degradation - that is a genuine refusal with item names in it.
A sweep clipped by `verification_max_obligations` is also a refusal, naming the
obligations it did not check, because an unchecked obligation is exactly where a
shrunken spec would hide.

### The fold: the part a future terminal must not "fix"

`harness.core.run_task` mints `status="success"` on exactly
`target_test_passed and regression_passed and not flaky`, and
`shared/types.py` (not this round's file) has no field for gate rows. So
`plan_fold` CLEARS `target_test_passed` and nothing else, and only when a
MANDATORY rung refused.

That is a deliberate, documented compromise, and it is a small lie about the
tests - which is why the reason ALWAYS travels in the same `raw_output` block.
Do not "fix" it by flipping `flaky` or by touching `regression_passed`; those
are worse lies. Do not add a promote path. The honest fix is an additive
`VerificationResult` field carrying the gate rows, which removes the need for a
boolean to stand in for a claim; that is filed below, not done here.

`applied` / `folded_fields` distinguish "the intelligence fold blocked this" from
"the baseline already refused", so the block is never read as a suite failure.
And the receipt is attached on ACCEPTING runs too: a verified run has to be able
to name the mechanism that claimed it.

### Two real bugs my own tests found (both fixed, both pinned)

1. **`receipt_rows` always reported the independent gate as passing.** A
   `GateVerdict` is a dataclass with no `__bool__`, so `bool(verdict)` was
   always `True` and `passed=True` for a gate that had refused. Found by
   `test_an_independent_judge_refusal_blocks_a_green_run`, which is the only test
   that can catch it because it is the only one with a green baseline AND a
   refusing judge. Same class of bug as `if not collection`.
2. **`obligation_rows` was unbound** when the spec rung was skipped, so a
   configured run with no spec artifact raised `UnboundLocalError` instead of
   reporting a skipped optional gate.

Also found by `ruff`: the E402/F401 pair from putting the key-tuple re-export
below the constants block. Fixed by moving it into the import block.

### Verification actually run (this tree, real Docker daemon 28.5.1)

- `python -m pytest tests/test_verification_gate_wiring.py -q -p no:randomly` ->
  **27 passed, 0 skipped**, 85-91s. The Docker-gated class drives the REAL
  sandbox for the baseline suites, the obligation sweep, and the held-out judge.
- `python -m pytest tests/test_verify.py tests/test_verify_js.py
  tests/test_feedback.py tests/test_git_output_rationale.py -q -p no:randomly` ->
  **124 passed, 0 skipped**.
- `python -m pytest tests/test_ceiling08_verification.py -q -p no:randomly` ->
  **78 passed** (the intelligence layer's own suite, unchanged and still green).
- `python -m pytest tests/test_sandbox.py tests/test_e2e_run_task.py -q
  -p no:randomly` -> **89 passed, 0 skipped**. This lane was recorded at
  88 passed + 1 failed for the `ToolLoopGuard` drift in the Ceiling-07 section
  above; that failure is gone, by another terminal's fix, not by this round.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0.
- `python -m ruff check` clean on `execution/verification_gate.py`,
  `execution/verify.py`, `tests/test_verification_gate_wiring.py`.
  `ruff format --check` clean on both NEW files.
- `python -m compileall -q execution/verification_gate.py execution/verify.py`
  -> exit 0.

### Blocked / not a pass

- **`tests/test_daily_driver_evals.py::test_quick_matrix_runs_real_comparison_with_receipts`
  fails: `feature_evidence.status == "failed"` (26/28 feature arms; the
  `agent_fetch_enabled` probe). NOT this round, and the proof is structural
  rather than remembered: `intelligence_config=` appears in exactly three files
  in the whole tree - `execution/verify.py` (the signature and the delegation),
  `execution/verification_gate.py`, and this round's test file. No production
  call site passes it, so the parameter is `None` on every production
  `verify()` call and the seam is unreachable. The failing probe also runs in a
  `python -m evals.daily_driver` SUBPROCESS, so nothing in the parent process
  can affect it. `harness/AGENTS.md` records the same feature as a
  cross-terminal drift point. Not counted as a pass and not "fixed" by weakening
  the assertion.
- **`execution/verify.py` is NOT `ruff format` clean** - one pre-existing
  long line in the target-rerun loop (`_report(res, expected_tests=..., sink=...)`,
  line 602), from a prior round, outside every hunk of this one. `verify.py` is
  a shared dirty file with other terminals' in-flight work, so it was NOT swept
  wholesale; `ruff check` on it is clean and both new files are format-clean.
- **No live-provider lane was run** and no credential was inspected, requested,
  or retained.
- **A dependency image build may need the network** on a cold per-repo cache; the
  Docker runs above reused the warm cache.
- `probe_logs/r2_01_seam_ab.py` is the A/B driver used while diagnosing the
  daily-driver failure. Its in-process monkeypatch could NOT have reached the
  subprocess feature lane, so it is recorded as weak evidence and the
  structural argument above is the one relied on.

### Not yet implemented

- **No production call site passes `intelligence_config`.** The seam is reachable
  only from tests today. See the requests below.
- **No `VerificationResult` field carries the gate rows**, so the fold has to
  borrow a boolean. See the requests below.
- **The obligation sweep costs one container run per sealed item.** Bounded at
  `verification_max_obligations` (8) and a clipped sweep refuses, but a 40-item
  spec cannot be checked in one gate today. The honest options are a single
  union run plus per-test resolution (needs per-node report data this repository
  does not produce) or a bigger budget; neither is built.
- **The judge is not wired into the repair loop's inner iterations.** It runs
  only on the gating call, which is the correct place for a verdict but means a
  reward-hacking shape is discovered late (at the gate) rather than early.
- **`execution.verification_intelligence.run_verification` is still not called
  from this seam.** It composes all five mechanisms including
  `test_selection` and `flake`; the delegated pipeline calls the ledger and the
  judge directly, so the import-graph selection and clean-environment flake
  confirmation remain reachable only from `evals/` and
  `tests/test_ceiling08_verification.py`. Folding them in is the obvious next
  step and is deliberately NOT done here - it would make the seam re-run the
  suite, and the non-recursion property is worth more than the extra coverage.
- **`changed_files` is threaded to the judge's claims but not used** to narrow
  or widen anything yet; the judge still evaluates the whole repository.

### Cross-terminal requests

1. **R2-04 (`harness/config.py`) - the DEFAULTS decision.** The 15 keys are
   currently absent from `DEFAULTS` on purpose, and
   `test_no_intelligence_key_is_in_the_harness_defaults` will FAIL if that
   changes. Please either (a) leave them out, which is the recommended
   behaviour-preserving choice, or (b) add the set deliberately and update that
   test with the reason, accepting that every task and every eval arm then
   enters the pipeline. Do not add a *subset* of them: a single default key
   silently switches every arm while a partial set reads as "the rest are
   defaulted" when they are not.
2. **Harness owner (`harness/core.py` - NOT mine to edit) - the wiring.** Pass
   the resolved config at the two gating call sites so the seam becomes
   reachable in production:
   - `core.py:1456` (final verifier gate) and `core.py:2089`/`2137` (agent-tests
     baseline/post-fix) call `verify(...)` today; add
   `intelligence_config=cfg` to the final-gate call at minimum.
   - The baseline call at `core.py:508` should probably NOT be gated: the
     baseline answers "did the target pass BEFORE any edit", and a sealed
     obligation set describes the finished state.
   - `verification_task_id=task.task_id` is what makes the gate's trace row
     land under the right task id; without it the row is emitted with no
     identity and the reason is recorded in the receipt instead.
   Please keep the three existing positional/keyword call shapes working - the
   new parameter is keyword-only with a `None` default, so passing nothing is
   unchanged behaviour.
3. **`shared/types.py` owner - an additive gate field.**
   `VerificationResult` needs an optional `verification_gates: List[Dict] = []`
   carrying `rung`/`name`/`passed`/`mandatory`/`reason`. It is additive with a
   default, so every existing constructor and every serialization stays valid
   (note `runtime/serialize.py` round-trips with an absent-key default - ask
   Terminal 3 to extend it). With that field the boolean fold in
   `verification_gate.plan_fold` becomes unnecessary, and
   `harness.core` can require `not v.verification_gates or all(g["passed"] for
   g in v.verification_gates)` instead of reading a claim out of
   `target_test_passed`. Until it exists, the fold is the only lever and it is
   documented as a compromise.
4. **`evals/daily_driver.py` owner.** The `verification_intelligence` feature
   probe calls `run_verification` DIRECTLY (line 4564) and never touches the
   seam. A second probe that drives the seam through `verify(..., intelligence_config=...)`
   would make the production wiring measurable in the eval harness rather than
   only in pytest. That is the change I would ask for next.
5. **`execution/verify.py` consumers.** `_intelligence_delegate` reads
   `execution.verify._autodetect_test_command` from the new module. That is a
   deliberate intra-family private seam (one language detector, never two) and
   is now declared in INTERFACES.md - but if a future round makes
   `_autodetect_test_command` public, update the import here and the INTERFACES
   note together.

---

## R2-12 - one ecosystem registry, and the zero-test refusal (2026-09-27)

**NEW `execution/ecosystems.py`. `execution/verify.py` consults it. The
vacuous-green class - a runner that exits 0 having collected nothing - is now
a distinct, blocking, LOUD outcome instead of a pass. One of the five required
proofs is BLOCKED (a real Go toolchain) and that is stated, not papered over.**

### 1. What is built

| surface | what it owns |
|---|---|
| `execution/ecosystems.py` (NEW) | THE per-language table: suite/build/lint/format commands, test globs, protected globs, structured-result format, zero-test policy + markers, toolchain, sandbox image, dep manifests, target-command template, protected runner-config surfaces |
| `execution/verify.py` (extended) | registry lookup, the toolchain probe, the structured-report capture, the `no_tests_collected` / `toolchain_unavailable` gates, the `## ecosystem` receipt |
| `harness/editor.py` (ONE additive kwarg) | `extended_protected_patterns(..., ecosystem=)` |
| `harness/config.py` (two `None` entries) | `ecosystem`, `ecosystem_protected_paths` - both behaviour-neutral |

**The registry is consulted, and there is still exactly ONE language detector.**
`python` and `javascript` are registered with `command_family` `pytest`/`jest`
and delegate command composition to `verify.py`'s pre-existing
`_autodetect_test_command` and `_target_command`. A language with no bespoke
detector uses `command_family="runner_template"` and composes from its own
fields. The registry owns the DECISION of which contract a repository is held
to; it does not fork the mechanics. That is why a Python or JS repository is
byte-identical to before this round - asserted by
`test_a_repository_the_registry_does_not_know_is_unchanged`, which requires the
dispatched command to be exactly `["python -m pytest -q test_m.py::test_x",
"python -m pytest -q"]` and requires no `## ecosystem` block in `raw_output`.

**Seven built-in ecosystems**, and `zig` is registered inside the test suite:
`go`, `rust`, `java`, `dotnet`, `ruby`, `javascript`, `python`. Adding a
language is a data change - `test_a_brand_new_language_gets_a_full_contract
_from_data_alone` registers a language that appears NOWHERE else in the tree and
gets detection, a suite command, a scoped target command, a zero-test refusal,
language-correct protected paths, and a published runner-config surface, with
no production code changed.

### 2. The two refusals, and why they are ENFORCED rather than documented

**`no_tests_collected`.** `execution.result_parsing.OUTCOME_NO_TESTS` is
promoted to a distinct gate-level outcome that appears in the receipt, in an
`ecosystem`-sourced `reports` row, and in a `## ecosystem` block appended to
`raw_output`. Not pass, not flaky, not skipped; `blocks_success()` is True and an
UNRECOGNISED gate value also blocks.

The policy is a CLOSED set of two values and BOTH are fail-closed:
`Ecosystem.validate()` raises `EcosystemPolicyError` for anything else, so there
is no "allow an empty suite" value a future language could be registered into.
`test_a_permissive_zero_test_policy_is_refused` parametrizes over
`allow_empty`/`none`/`permissive`/`""`/`FAIL_CLOSED`/`None` and requires the
refusal.

`fail_closed` additionally requires POSITIVE collected-test evidence before a
zero exit may be called a pass, plus the ecosystem's own `zero_test_markers`.
**Measured on this round's own Go fixture:** `parse_test_run` reports
`outcome="pass", tests_collected=None` for the `[no test files]` capture - the
exit code really does call it a pass - and `gate_outcome` reports
`no_tests_collected`. The refusal is doing work, not restating a verdict the
parser had already reached, and
`test_the_exit_code_alone_reports_this_run_as_a_pass` pins the defect so the
suite cannot quietly become theatre.

Three shapes are deliberately NOT "zero tests collected", because each would be
the same vacuous green wearing a different hat, and all three are pinned:

- a **Go build failure** (a `build-fail` event) is `error`, not an empty suite;
- a **package-level failure with every named test passed** (a teardown or
  post-test panic) WITHHOLDS the structured report, so the exit code decides -
  feeding the counts would hand `parse_test_run` a green report for a
  non-zero exit;
- a **missing toolchain** is `toolchain_unavailable`, deliberately NOT conflated
  with `no_tests_collected`. One is a broken machine, the other is a suite that
  ran nothing, and telling an operator to delete a test suite that is fine is
  the worse of the two mistakes.

**The toolchain probe.** Each ecosystem declares its binaries;
`toolchain_probe_command()` builds a `command -v` check that is PREPENDED to
the runner command, so it costs no extra container and cannot change a present
toolchain's exit code. A missing binary prints the stable
`TOOLCHAIN_UNAVAILABLE_MARKER` and exits 127 - the shell's own command-not-found
code, so a reader that ignores the marker still sees a non-zero exit. The marker
is matched BY NAME, so neither a paraphrase of the message nor a runner that
merely printed a similar word can trigger it.

### 3. The JUnit XML channel is FED

`parse_test_run(junit_xml=..., json_report=...)` already existed and had **NO
production caller anywhere in the tree** - the channel was present and inert.
Now:

- For a `sentinel` ecosystem the composed command appends the runner's report
  argument, **preserves the runner's own exit code across the report echo**
  (`{ cmd; } ; __vex_rc=$? ; ... ; exit $__vex_rc` - appending naively would make
  every run exit 0, the most catastrophic possible version of this feature), and
  echoes the report inside a **per-call sentinel block** keyed by an unguessable
  token, so test OUTPUT cannot forge a report. `split_report()` takes the LAST
  start marker and the first end marker after it, and restores `raw_output` to
  exactly the runner's own bytes - which is what keeps the `execution.feedback`
  and `execution.rationale` transcript parsers unaffected.
- The report is written **inside the container** (`/tmp/...`, the sandbox's
  tmpfs), never into the bind-mounted repository, so it cannot become part of
  the delivered diff. `test_the_report_is_written_inside_the_container_never_
  the_repository` pins the path.
- Go uses `CAPTURE_INLINE`: `go test -json` IS Go's own event stream, so no
  extra tool is needed. `parse_go_test_json` normalizes it into the same channel
  shape `parse_pytest_json` already produces.
- **Proven with a REAL runner:** a real `python -m pytest --junitxml` run in a
  real container. Both `reports` rows read `source="report"`,
  `confidence="high"`, with the runner's own collected/skipped counts
  (`[1, 3]` for the target node id and the full suite respectively), and the
  document's own `<testcase>` elements reach the receipt. This was a real
  defect this round's own test caught, not a review finding: the sentinel
  extraction left a leading newline, an XML declaration must be the document's
  FIRST byte, so `ET.fromstring` raised `ParseError` and the whole channel
  silently degraded to prose. Fixed at the transport boundary and again
  defensively in `normalize_structured`.

**Per-test outcomes reach the receipt.** The runner's own rows ride on
`result.test_outcomes` (JSON-safe; the block bounds the display at
`MAX_RECEIPT_CASES` and always prints the true total, so a truncated list is
never mistaken for a complete one), and the FAILING ones are MERGED into the
existing `structured_feedback` AFTER the prose parser has run - so the
model-facing channel is sharpened, not replaced.

### 4. Per-language protected paths, and the hole that was there

`DEFAULTS["protected_paths"]` is `["tests/*", "test_*.py", "*_test.py"]`.
`test_the_harness_default_protects_no_java_or_go_test_file` proves it matches
**no** Java or Go test file: a Maven or Go repository had **no protected test
surface at all**. `effective_protected_paths(configured, eco)` KEEPS the
operator's globs (they are policy, not a default) and ADDS the ecosystem's own;
`harness.editor.extended_protected_patterns` gained ONE additive keyword-only
`ecosystem=` to reach it. `is_protected`'s two-positional-argument signature is
UNCHANGED, and `extended_protected_patterns` still has no production caller.

The set is language-SPECIFIC, not merely larger:
`test_the_two_ecosystems_do_not_protect_each_others_tests` requires a Java glob
set NOT to protect `add_test.go` and a Go glob set NOT to protect
`src/test/java/...`.

### 5. Config discipline

Two `None`-valued `DEFAULTS` entries, and `None` is behaviour-neutral because it
is what an ABSENT key already means to both consumers.
`test_no_ecosystem_default_switches_every_run` iterates `DEFAULTS` and fails if
either key is ever given a real value. `DEFAULTS["protected_paths"]` itself is
untouched and pinned by `test_protected_paths_default_is_untouched_by_this_
round` - changing it would either weaken the Python guarantee (drop the globs)
or over-block every other language (ship every ecosystem's globs at once).

### 6. Verification actually run (this tree, real Docker daemon 28.5.1)

- `python -m pytest tests/test_ceiling_r2_12_polyglot.py -q -p no:randomly` ->
  **79 passed, 1 skipped**. All five required proofs are present and named.
  **Of the four that ran, two are real-Docker proofs**: a real pytest JUnit
  report consumed with its per-test outcomes into the receipt, and a real Go
  repository in the real sandbox degrading to `toolchain_unavailable`.
- `tests/test_verify.py tests/test_verify_js.py tests/test_feedback.py` ->
  **93 passed**.
- `tests/test_ceiling08_verification.py` -> **78 passed**.
- `tests/test_verification_gate_wiring.py tests/test_ceiling_r2_02_flake.py
  tests/test_ceiling_r2_05_baseline.py tests/test_ceiling_r2_03_config_guard.py`
  -> **176 passed, 1 failed** (see section 8).
- `tests/test_editor_prompts.py tests/test_config_trace_state.py
  tests/test_stubs_and_deps.py tests/test_adversarial.py
  tests/test_ceiling_r2_06_editing.py tests/test_ceiling_r2_07_codemod.py` ->
  **207 passed, 1 skipped** (the skip is a Windows symlink-privilege case).
- `tests/test_sandbox.py` -> **61 passed**.
- `tests/test_e2e_run_task.py tests/test_git_output_rationale.py` ->
  **59 passed** (the full verified-fix loop, real Docker).
- `tests/test_modes.py tests/test_coordination_e2e.py` -> **104 passed**.
- `python -m evals.run --check` -> **14/14 CLEAN**.
- `python -m evals.run --suite daily-driver --no-docker --json` ->
  **50/52 case arms ok**, `pass_count 50 / fail_count 2`,
  `zero_false_verified_successes=true`, `zero_unauthorized_mutations=true`,
  `zero_lost_edits=true`, **28/28 feature-evidence arms pass**, `uncovered:
  []`. Readiness `NOT_READY`: the Docker and live-provider lanes were not
  selected, `cost_latency_user_intervention_at_least_90pct` is unmeasured
  because `manual_corrective_follow_up_count` is `null`, and the sampled manual
  -repair evidence is `incomplete`. Report:
  `logs/evals/20260927-010200-94c1e66620c74bc2a280c583487595da/daily_driver_report.json`.
- `python -m ruff check` clean on every owned file; `python -m compileall -q`
  clean; both NEW files (`execution/ecosystems.py`,
  `tests/test_ceiling_r2_12_polyglot.py`) are `ruff format` clean. No formatter
  was run on the shared dirty files `verify.py`, `editor.py` or `config.py`
  (per the shared-file protocol), so `verify.py` retains pre-existing
  whole-file format debt outside every hunk of this one.
- **No live-provider lane was run** and no credential was inspected, requested
  or retained.

### 7. What is NOT implemented - do not assume it

- **THE GO TOOLCHAIN LANE IS BLOCKED.** `execution/sandbox.py` has no Go base
  image and **no hook for one**: `BASE_IMAGE`/`BASE_IMAGE_NODE` are read from
  the environment at import, `_detect_repo_language` returns `"python"` for a Go
  repository, and `ensure_image()` builds from the pytest base. So a real
  `go test` cannot run in the sandbox, and
  `test_a_go_repo_with_a_real_failure_is_detected_and_a_real_fix_verifies` is
  IMAGE-gated and **self-skips here**. A skip is BLOCKED coverage, never a pass.
  `execution/sandbox.py` is another terminal's in-flight edit and was NOT
  touched; the exact request is section 9 (1).
- **Per-language protected paths are reachable but NOT yet on the run path.**
  `effective_protected_paths` and `extended_protected_patterns` are complete and
  tested, but the two resolution points in `harness/core.py` still read
  `cfg.get("protected_paths")` directly (`:669` and `:2540`, each followed by the
  `_authorized_test_target` strip). Until that wiring lands, a Go or Java
  `run_task` still enforces the Python-shaped list. Section 9 (2).
- **`VerificationResult` has no real fields for the receipt.**
  `shared/types.py` is another owner's file and was not edited, so
  `ecosystem`, `ecosystem_language`, `ecosystem_gate`,
  `ecosystem_blocks_success`, `no_tests_collected`, `toolchain_unavailable`,
  `zero_test_policy` and `test_outcomes` are ADDITIVE INSTANCE ATTRIBUTES (the
  `execution/flake_gate.py::attach_evidence` precedent). Consequence:
  `runtime/serialize.py` does NOT round-trip them, so a replayed journal loses
  the ecosystem receipt.
- **JavaScript has no structured channel.** jest's `--json` is deprecated and
  vitest's junit reporter needs a dependency, so its `result_format` is honestly
  `prose` and the capture is off. The Python-shaped default list also still
  applies to a JS repository, so `tests/*` and `__tests__/*` are both protected.
- **Rust / Java / .NET / Ruby are registered as DATA but never RUN.** Their
  commands, globs, policies, toolchains and images are exercised by the
  composition and data-change tests; no test lane runs `cargo`, `mvn`, `dotnet`
  or `bundle`.
- **The lint and format commands are declared, not executed.** No production
  caller runs `Ecosystem.lint_command` / `format_command`; the registry records
  them because the prompt asked for them and a future lint gate can read one
  source.
- **`is_test_path` is a helper, not a gate.** Nothing in the pipeline calls it
  yet; `harness.editor.is_protected` remains the enforcement point.
- **An `Ecosystem` is process-global.** `register_ecosystem` is deliberately
  not thread-safe and mutates module state; the built-ins register at import
  time with `replace=True`. A concurrent registration of the same name is a
  configuration bug, not a supported operation.
- **One flake-label semantic did widen, conservatively.** The target-rerun
  labels are now gate outcomes, so a run that the parser called `error` (a
  crash-shaped capture) is labelled `error` where it used to be labelled `fail`.
  A `fail`/`error` MIX across reruns is therefore now `flaky=True` where it was
  `False`. That can only ever refuse, never pass, and no existing test moved.

### 8. Two red results, both PROVEN not from this round, neither weakened

**(a) `tests/test_ceiling_r2_02_flake.py::test_the_added_wall_time_of_
repetitions_is_measured_and_linear`** - a host wall-clock pin
(`elapsed(3 reps) <= 3*elapsed(1 rep) + 1.0`). On the loaded host it measured
7.48s against a 7.09s bound. It **passes 3/3 standalone** (9.2s, 9.9s, 8.0s).
Structural argument rather than recollection: it exercises `execution.flake_gate`
over `execution.flake.run_local_command`, the HOST lane, which this round does
not touch, and the only references to `execution.verify` in either
`flake_gate.py` or `flake.py` are DOCSTRINGS (`:8` and `:398`). Not counted as
a pass and not "fixed" by relaxing the bound.

**(b) `dd_20_live_tui_status_diff`** fails both arms on
`live_diff_populated` - a Textual transcript poll for the literal string
`+value = 2` (`evals/daily_driver.py:2570`). **Controlled A/B, not
recollection:** with `execution.verify.detect_ecosystem` forced to return `None`
through a `sitecustomize.py` hook on `PYTHONPATH` - so the driver AND every
worker subprocess take the byte-identical pre-R2-12 path, with no probe, no
report wrapper, no ecosystem block and the same dispatched command - the arm is
**still `failed` with `live_diff_populated=false`**. R2-06's own AGENTS.md
already records this arm as timing-flaky under load. The remaining matrix
numbers in section 6 are reported exactly as measured.

**(c) Disclosed edit outside this round's file list.** Three assertions in
`tests/test_ceiling08_verification.py::TestIncrementalSelection` pinned the
literal DISPATCHED command, which legitimately changed once the toolchain probe
and the report echo became part of what reaches the sandbox. They were made
**STRONGER, not weaker**: they now assert the claim those tests were written for
directly - the full suite ran AND no test path leaked into the gating run -
through a documented `_runs_the_full_suite_only` helper, instead of by exact
string equality. That form fails if any test path appears whether or not a
wrapper is present. No assertion was relaxed and no other file outside this
round's set was edited.

### 9. Cross-terminal requests

**(1) `execution/sandbox.py` - a Go base image, and consulting the registry
instead of its own two-entry table. This is the ONE change that makes the Go
lane real.** The file is dirty (another terminal is in it right now) and was not
touched. The smallest correct change:

```python
BASE_IMAGE_GO = os.environ.get("HARNESS_SANDBOX_BASE_IMAGE_GO", "golang:1.23-slim")

def _ecosystem_of(repo_path: str) -> str:
    """Classify a repository for IMAGE selection, via the one registry."""
    try:
        from execution.ecosystems import detect_ecosystem
        found = detect_ecosystem(repo_path)
    except Exception:
        return "python"
    return found.name if found is not None else "python"
```

then have `_detect_repo_language` return that name (keeping the `"js"` alias the
JS path expects), add a `golang_base_image_tag()`, and add the Go branch to
`_dep_image_tag` so the fingerprint cannot collide with the Python family (it
must include the new base tag, as the JS family does). `Ecosystem.sandbox_image`
and `Ecosystem.dep_manifests` already carry the data - `("go.mod", "go.sum")` and
`"golang:1.23-slim"` - so nothing else in this module needs to change. Once that
lands, un-skipping the Go proof is deleting the one
`@requires_go_image` marker; no other test change is needed.

**(2) `harness/core.py` - the per-language protected-path resolution, and it is
TWO call sites.** `core.py:669` and `core.py:2540` both do
`protected = [str(p) for p in (cfg.get("protected_paths") or [])]`, and both are
followed by the `_authorized_test_target` strip that removes
`{"tests/*", "test_*.py", "*_test.py"}`. Both should become:

```python
from execution.ecosystems import effective_protected_paths
protected = list(effective_protected_paths(
    cfg.get("protected_paths"),
    eco=cfg.get("ecosystem") or None,   # a NAME pins it; None autodetects
))
```

Note the `_authorized_test_target` strip must then drop the ECOSYSTEM's globs
too, not the three Python literals, or a declared "fix the tests" task would be
refused on `*_test.go` while being allowed on `tests/*`. `eco=` accepts a name
string, so a caller can pin the ecosystem without importing the module. The
`ecosystem` config key is already in `DEFAULTS` as `None`, and
`eco.ecosystem(None)` returns `None`, so this is behaviour-neutral for every
task that does not set it.

**(3) `harness/agent_loop.py` - the same resolution at the interactive tool
layer.** `agent_loop.py:1458` and `:2190` read `cfg.get("protected_paths")` for
the EDIT/WRITE refusals (`:2335`, `:2369`) and the BASH change audit (`:1794`).
An agent editing a Go repository in a session today has no `*_test.go`
protection. `extended_protected_patterns(protected, ecosystem=cfg.get(
"ecosystem") or None)` is the one-line form and is additive.

**(4) `shared/types.py` + `runtime/serialize.py` - real fields for the
receipt.** `VerificationResult` needs `ecosystem: str = ""`,
`ecosystem_gate: str = ""`, `no_tests_collected: bool = False`,
`toolchain_unavailable: bool = False` and
`test_outcomes: List[Dict] = field(default_factory=list)`, all additive with
defaults so every existing constructor stays valid. Until they exist the
attributes are lost on a journal replay, which is the only real cost of the
attribute route. `runtime/serialize.py` needs the same round-trip treatment
`structured_feedback` got.

**(5) `cli/doctor.py` - a toolchain section is now cheap to add.** The registry
knows each ecosystem's binaries and `doctor` already has a per-check `probe`
contract. A `check_toolchains` row would answer "why did my Go task report
`toolchain_unavailable`" without reading a trace. Not required by this round.

**(6) `evals/daily_driver.py` owner - a polyglot capability probe.** A third
case arm family for a Go or Java repository would make the per-language
protected paths and the zero-test refusal measurable in the matrix rather than
only in pytest, the way `dd_26` does for the LSP. This round's registry makes
that cheap: a fixture repo plus `"ecosystem": "go"` in the arm config. The Go
fixture is BLOCKED on request (1); a Java fixture is not, since its
`toolchain_unavailable` degradation is itself the honest outcome to measure.
---

## VEX-PF-10 - reachability: the flake gate and the baseline set are on the live path (2026-09-29)

**`execution/verify.py` only. `execution/flake_gate.py`,
`execution/baseline_set.py`, `harness/config.py`, `shared/types.py` and every
verifier mint were NOT edited. The two new parameters are keyword-only with
`None`/`""` defaults, so every existing call site is byte-identical. No Boundary
0-5 signature, event kind, serialized field, completion status, or verifier mint
changed.**

### 1. What was dark, and why "wired" is not evidence

`execution/flake_gate.py` and `execution/baseline_set.py` shipped with ZERO
production importers. The flake gate therefore could not fire: with
`baseline_reruns=1` the run count was 1 for every configured value, so
`len(set(outcomes)) > 1` was unsatisfiable and a test that passed once and
failed later was reported as a clean, non-flaky pass. `execution/baseline_set.py`
was never called, so "was this test already broken before I touched it?" was
never answered and an environment failure looked exactly like a code failure.
This is the third time this project has shipped a mechanism nothing imports;
the gate that now prevents a fourth is
`tests/test_module_reachability.py`.

### 2. The seam, and why it is a THIRD config parameter

```python
verify(..., intelligence_config=None,   # REPLACES this body (R2-01, unchanged)
            rung_config=None,           # AUGMENTS this body  <- new
            run_dir="", phase=None)     # the caller's facts  <- new
```

`intelligence_config` had to stay exactly what it was. It REPLACES this
function's body: when it carries an intelligence key the call is delegated
wholesale to `execution.verification_gate`, which re-enters `verify()` with
`intelligence_config=None` for the baseline. A rung hung off it would therefore
be invisible on the delegated path. So the new rungs read a separate
`rung_config` - the resolved `Task.config` - and the two never conflate.

`run_dir` and `phase` are parameters rather than config keys because they are
facts only the CALLER knows. `verify()` is a stateless evaluator of one repo
state; inferring "is this the pristine tree or the edited one" from `repo_path`
would be false precision, so the caller declares it and an absent or
unrecognised `phase` leaves `baseline_set` dark rather than asking it a question
it cannot answer.

### 3. The keys, and the rule that keeps them opt-in

`FLAKE_GATE_CONFIG_KEYS = ("post_fix_reruns", "flake_repetitions_cap")` and
`BASELINE_SET_CONFIG_KEYS = ("baseline_set_enabled", "environment_triage_enabled",
"baseline_set_max_failures", "baseline_set_max_summary_chars")` live HERE, in
the seam holder, for the same reason `_INTELLIGENCE_CONFIG_KEYS` does: the
key-presence question must be answerable without importing the thing it gates.

`baseline_reruns` is deliberately NOT in the flake tuple even though
`repetitions_for_stage` falls back to it, because it IS in
`harness/config.py::DEFAULTS` and its mere PRESENCE in a merged config would
switch every task and every eval arm onto the gate.
`test_no_reachability_key_is_in_the_harness_defaults` fails if any of the six
ever gets a value there.

### 4. The repetition LOOP is deliberately NOT routed through the gate

This is the one thing a future editor must not "simplify". The obvious wiring
is to replace the manual `for _ in range(run_count)` loop with
`execution.flake_gate.evaluate_repetitions`, and it is WRONG here:
`observe_repetitions` records a RAISING repetition as `error` and continues,
while `verify()`'s callers depend on a raising sandbox
(`SandboxUnavailableError` must keep propagating so the harness reports a task
error rather than a failed test). The loop is therefore unchanged and the PURE
`flake_verdict` reducer is used on the outcomes the loop already collected.

Consequences, all measured or asserted:

- The labels are the loop's own **ecosystem gate** vocabulary (`pass`, `fail`,
  `timeout`, `no_tests_collected`, `toolchain_unavailable`, `error`), not
  `classify_run`'s. `flake_verdict` only needs distinctness, so the R2-12
  refusals are preserved - folding them into `pass`/`fail` would be a weakening.
- `attach_evidence` sets `flaky = verdict.flaky`, which is
  `len(set(outcomes)) > 1` for two or more repetitions and `False` for one:
  the same value the historical code computed, always.
- `_resolve_run_count` returns the historical `max(1, rerun_for_flake_check)`
  when no flake key is present, so the unconfigured repetition count is
  unchanged byte-for-byte.

### 5. The fold can only CLEAR, and that is the whole safety argument

`_baseline_set_rungs`'s post-fix branch assigns `result.target_test_passed =
False` and nothing else. There is no assignment of `True` to any
`VerificationResult` field anywhere in the seam, and
`test_the_filed_call_site_is_now_applied_and_still_key_presence_gated` asserts
that by reading the source of the seam function. The fold is additionally
conditioned on `BaselineVerdict.blocks_success`, which is independently True
for the same evidence - the fold is the braces, the property is the belt, and a
caller that ignored the fold entirely still could not mint a success.

The R2-05 filed handoff also wanted `should_stop_for_environment` to end a run
as `status="error"` on a non-repairable machine fault. That is a HARNESS
decision (it changes a run's terminal status) and is filed below rather than
applied here.

### 6. Which rung produced this verdict, on the receipt AND in the trace

`verify()` is a stateless evaluator and the same three booleans come out of
several mechanisms, so "which gate said this was verified?" had no answer
anywhere except the surrounding code. Every return path now calls
`_record_rungs`, which:

- sets the additive instance attributes `verification_rung` (the primary rung)
  and `verification_rungs` (every rung that participated, in order);
- appends a bounded `## verification-rung` block to `raw_output` LAST, after
  every other receipt, so it cannot be shadowed by one that renders after it;
- emits one `shared.tracing` `verification_rung` row.

`RUNG_NONE` exists for the "no suite command could be resolved" path, and the
ungated path is NAMED (`baseline`) rather than left blank: "no gate was
configured" and "we forgot to record it" must not be the same bytes. The
closed vocabulary is `VERIFICATION_RUNGS`.

### 7. MEASURED cost (this host, this tree, warm image, real Docker)

Real `execution.verify.verify` against a 2-test fixture, 3 samples each,
`ensure_image` warmed first:

| `post_fix_reruns` | median wall | `flake_check` | `repetitions` | rung |
|---|---|---|---|---|
| absent (1 run) | **5.26 s** (4.73 / 5.34 / 5.26) | *absent - no receipt* | *absent* | `baseline` |
| 2 | **7.11 s** (7.11 / 6.32 / 7.74) | `not_flaky` | 2 | `flake` |

**+1.85 s median (+35%)** for the second repetition on this fixture, and the
unconfigured run is *observably* untouched: it carries no `flake_check`
attribute at all, which is what `test_the_no_config_path_is_byte_identical`
asserts. The seam's own host-side cost - the key lookups, the verdict
reduction, the receipt block and the trace call, with the container round trip
replaced by a stub - measures **0.618 ms min / 0.712 ms median** per `verify()`
call (N=400 x 5, `Temp/pf10_cost.py`). Both numbers exclude the test's own
runtime, so for a real target test scale the +1.85 s by the test, not by this
fixture.

The per-task MULTIPLIER is the number that matters operationally:
`harness/core.py` verifies the target after every step turn, so a configured
`post_fix_reruns` is paid up to 15 times on a long task. That is the
configured cost and it is now reachable; the per-step checkpoint is NOT a
completion gate, and its receipt honestly says which rung claimed it.

### 8. Verification actually run (this tree, `-p no:randomly`, real Docker 28.5.1)

- **Required lane** `test_ceiling08_verification` + `test_ceiling_r2_02_flake` +
  `test_ceiling_r2_05_baseline` + `test_module_reachability` -> **166 passed**
  (151.68 s).
- `test_verify` + `test_verify_js` + `test_verification_gate_wiring` +
  `test_feedback` -> **120 passed** (312.16 s).
- `test_e2e_run_task` -> **28 passed** (397.01 s): the whole verified-fix loop
  through a real container, against the modified `harness/core.py`.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0. No prompt changed,
  so the prompt-regression matrix is unchanged by construction as well as by
  measurement.
- `ruff check` clean on all five owned/edited files; `compileall` clean.
- **No live-provider lane was run** and none is claimed; no credential was
  inspected, requested, or retained.

### 9. ONE disclosed edit outside this round's file list

`tests/test_ceiling_r2_05_baseline.py::test_nothing_in_this_round_touched_the_
verifier_or_the_run_loop` asserted `"baseline_set" not in execution/verify.py`
- a deliberately self-arming pin that was designed to FAIL the day somebody
applied the handoff it filed. It failed, which is the pin working. It is
REPLACED by `test_the_filed_call_site_is_now_applied_and_still_key_presence_
gated`, which is strictly stronger: the old test proved the wiring was ABSENT,
the new one proves it is PRESENT, that it cannot fire without an operator's
key, that its only effect on the result is to CLEAR a boolean, and it keeps the
original module-level invariants verbatim. No assertion was relaxed.

### 10. Not implemented, stated plainly

- **`should_stop_for_environment` is not consulted by any harness loop.** The
  verdict rides the receipt and the `reports` rows, and a non-repairable
  environment fault is visible there, but nothing ends a run as
  `status="error"` on it. That is a `harness/core.py` decision about terminal
  status and is the top request below.
- **The delegated intelligence path does not carry the new rungs.**
  `verification_gate.run_intelligent_verify` re-enters `verify()` without
  `rung_config` (it must, or the recursion would not terminate on the
  parameter), so a run that opts into BOTH the intelligence pipeline and the
  flake gate gets the pipeline's own `baseline` rung and the repetition count
  its caller resolved. It is not a silent skip - the caller resolved the count
  and it is in the receipt - but the three-valued verdict is absent there.
  One parameter threaded through `verification_gate` closes it; that file is
  not this round's.
- **`intelligence_config=cfg` is still not passed at any `harness/core.py`
  call site.** R2-01 filed that as its own request and it is untouched here.
- **No `harness/config.py` key was added.** All six remain absent from
  `DEFAULTS` on purpose. A `None`-valued discoverability entry would be
  behaviour-neutral and is the config owner's call.
- **The pre-existing per-step repetition cost is unchanged**: with no
  `post_fix_reruns` key every call site still passes
  `int(cfg.get("baseline_reruns", 1))`.

### 11. Cross-terminal requests

1. **`harness/core.py` (the integrator) - ONE call, and it is the point of the
   baseline-set module.** After the final gate, when a `baseline_set_*` key is
   present, `execution.baseline_set.should_stop_for_environment(verdict)`
   returns a `status="error"` end state for a non-repairable machine fault
   (absent daemon, missing declared dependency, unreachable network). The
   receipt is already on the result as `baseline_set`; this is the one line
   that turns "visible" into "the run stopped instead of burning attempts on a
   working tree that was never broken". It is a change to a run's terminal
   status, which is why it is not applied here.
2. **`execution/verification_gate.py` (R2-01's) - ONE parameter.** Thread
   `rung_config` from `run_intelligent_verify` into its inner
   `verify_module.verify(...)` call, the same way `intelligence_config` is
   threaded today. The recursion still terminates because the INNER call must
   keep `intelligence_config=None`; the new parameter is not a delegation
   trigger. Without it a run that configures both gets the pipeline's verdict
   and not the flake gate's.
3. **`harness/agent_kernel/completion.py:97`** still reads
   `int(self.config.get("baseline_reruns", 1))` directly. It is the typed
   kernel's own `baseline_reruns` read and the same stage decision; that file
   is not this round's.

---

## P1/W1-T2 � the verification path is measured, the walks are inside the bar, and `partial` cannot read as `complete` (2026-10-02)

**Phase:** P1 Daily Usable, Wave 1, T2 (execution). **NEW:** `execution/walk_scope.py`,
`execution/test_w1_latency_honesty_pins.py`. **EDITED:** `execution/verify.py`,
`execution/sandbox.py`, `execution/test_selection.py`, this file.
**`harness/**`, `runtime/**`, `cli/**`, `shared/**`, `memory/**`, `tests/**` and
`evals/**` were READ and NOT WRITTEN.** No Boundary 0-5 signature, event kind,
serialized field, completion status or verifier mint changed, and
`shared/types.py` was **NOT** edited � every new field is an ADDITIVE INSTANCE
ATTRIBUTE, the `execution/flake_gate.py::attach_evidence` precedent.

### 0. The headline, and the number it came from

The brief's premise was that wiring `test_selection` into the verification path
would "turn a full suite into the tests that could possibly be affected". On this
repository, as the code stood, it would have done the opposite.

| | before | after |
|---|---:|---:|
| `test_selection.build_import_graph` on THIS repo | **495.3 s** | **10.6 s** cold / **0.107 s** warm (digest-cached) |
| directories the walk opens | **149,559** | **125** |
| "modules" the import graph discovered | **39,599** | **556** |
| of those, REAL source files | **569** | 556 |
| verification wall, `regression_mode="full_suite"` | 7.28 s | 7.28 s (unchanged � the default lane is byte-identical) |
| verification wall, `regression_mode="target_only"` | n/a | **3.00 s** |

**98.6% of the import graph was harness log artifacts.** `logs/` is this
project's own run directory: one subdirectory per run, each holding a
`pristine/` and a `work/` **copy of the repository under repair**. The old walk
did not prune it, so `select_tests` was selecting tests out of snapshot copies
of the tree it was meant to analyse. This is a CORRECTNESS finding that happened
to be found as a latency one, and it is the most important thing this round did.

### 1. The 851x, and the ONE authority

Measured 2026-10-02 on this tree (Python 3.10.11, win32) by wrapping
`os.scandir` at the `os` module level and counting directory opens:

| authority | entries | dirs opened | scandir seconds |
|---|---:|---:|---:|
| `memory/code_graph.SKIP_DIR_NAMES` | 20 | **171** | 0.017 s |
| `harness/retrieval._SKIP_DIRS` (at session start) | 14 | **145,552** | 54.75 s |
| `execution/test_selection._SKIP_DIRS` | 14 | **145,552** | 43.70 s |
| **`execution/walk_scope.SKIP_DIR_NAMES`** (NEW) | **49** | **125** | **0.032 s** |

**Directory-op counts carry the claim, not seconds.** Seconds inside one process
are confounded by page-cache warming � the same bare walk measured 14.1 s and
0.4 s in one run depending on order � so seconds are supporting evidence and the
op count is the measurement. It is also the unit the brief's own bar is written
in ("exceeds 1s or 1000 entries").

`execution/walk_scope.py` is the **re-converged union** of all three tables,
adopted **mid-session**: T1 widened `harness/retrieval._SKIP_DIRS` from 14 to
45 entries while this round was running (adding exactly `Temp`, `graphify-out`,
`probe_logs`, `out`, `target`, `vendor`, `tmp`), and this table was re-read to
include them rather than to race them. **All three authorities now AGREE** and
`missing_from()` returns `()` in both directions, which is what T1's handoff
predicted. The pin `test_the_authority_is_a_superset_of_every_foreign_table`
enforces it and will fail the day one drifts.

`execution/snapshot.py` is deliberately EXEMPT and must stay so: it is a COPIER
and must never prune real committed content. A separate pin already forbids a
table there.

### 2. `verify(..., regression_mode=...)` � the fast lane, honestly named

New keyword-only, default `full_suite` = **byte-identical to the historical
behaviour**. An unrecognised value **RAISES** rather than defaulting.

| mode | `regression_passed` | `regression_check` | what it ran |
|---|---|---|---|
| `full_suite` (default) | `True` | `complete` | every test |
| `selected` | `True` | **`partial`** | the import-graph selection |
| `selected`, no usable selection | **`False`** | `not_run` | **nothing** � refuses rather than silently running the full suite |
| `target_only` | **`False`** | `not_run` | **nothing** |

`regression_passed` is `False` in the `not_run` rows because a missing
measurement is not a passing measurement (`phases/DOCTRINE.md` �1). The
`regression_check` string is what says WHY, and it is a FOUR-value closed set:
`complete | partial | not_run | unavailable`. The bool stays fail-closed; this
is the field that makes it legible.

**`selected` reporting `partial` was a bug in this round's own first
implementation, and it is the pin that now exists for it.** That version decided
completeness from the selection's STRATEGY, so a sound `import_graph` selection
of 2 of 3 tests reported `complete`. Soundness and completeness are different
axes and are now two separate methods:

* `coverage_is_complete()` � decided on the NUMBERS (`selected >= total`),
  plus missing files, a bounded walk, and a scope that actually excluded
  something.
* `coverage_is_sound()` � decided on the STRATEGY (`import_graph` yes;
  `bounded` / `same_package` / `fallback_all` no).

`fallback_all` selects everything and is therefore COMPLETE while being UNSOUND.
Reporting that as partial would train every reader of the flag to ignore it,
which destroys the only thing it is for.

### 3. The cost receipt (T2.W1.4)

`result.verification_cost` publishes, per verification: `wall_s`,
`sandbox_overhead_s`, `container_s`, `overhead_s`, the per-phase `phases` dict,
`command_runs`, `outcomes`, `regression_mode`, `regression_scope`,
`regression_check`, `suite_command`, `regression_command`, `tests_selected`,
`tests_total`, `tests_skipped`, `tests_skipped_reason` and `coverage`.

`execution/sandbox.py::_with_phases` now times every phase of one call and sets
`elapsed_s` / `container_s` / `overhead_s` / `phases` on the result.
**`container_s` INCLUDES the command's own runtime** and the receipt says so in
its own `note`; a reader subtracting the wrong thing gets a wrong answer.
`overhead_s` is `elapsed_s - container_s` � the cost of *reaching* a sandboxed
command.

A phase the call did not reach is **ABSENT** from `phases`, not `0.0`. A zero
reads as "measured and free".

**Two ordering bugs this round's own tests found, both silent, both worth the
cost of the round:**

1. `execution/ingress.py::seal_streams` returns a NEW dataclass (it rebuilds
   field-by-field so no field is dropped), which **discards instance
   attributes**. Stamping the timing before sealing threw it away and every call
   reported no timing at all. `_with_phases` therefore seals FIRST and stamps the
   returned object, including the `REDACTION_UNAVAILABLE` substitute.
2. `verify.py:624` rebuilds `ExecutionResult` the same way on the sentinel-report
   path, with the comment *"A NEW result rather than a mutation"* � which is
   correct and also dropped the timing. The receipt read `0.00s` for real
   three-second runs. `_carry_sandbox_timing` copies the four attributes across,
   and `SANDBOX_TIMING_ATTRS` names them so the list cannot drift from the copy.

### 4. `warm_sandbox.py` is UNWIRED, and wiring it to verification is FORBIDDEN

The brief asks to "warm the image ... wire it ... report the saving". Those are
two different mechanisms and conflating them would weaken containment:

* **IMAGE warming is `ensure_image`, and it IS wired** �
  `execute_sandboxed:2409` calls it on every call. Measured 0.711 s first,
  **0.255 s** warm. It resolves a manifest-only fingerprint.
* **`warm_sandbox.py` is CONTAINER reuse** � one long-lived container per
  (repo, task), for the agent step loop. It has **zero production call sites**
  (only `execution/__init__.py`'s re-export and the P0 pins), and it **refuses
  `purpose="verification"` AT CONSTRUCTION**, verified live:
  *"warm sandbox cannot serve purpose 'verification'; the final verification
  boundary must keep a fresh container per command"*.

So it was **not** wired, and the number it would have saved is measured anyway:

| | fresh `--rm` per call | warm container `docker exec` |
|---|---:|---:|
| median per command | **2.067 s** | **0.318 s** (85% faster) |
| first call | 2.067 s | **1.677 s** (container start + first exec) |

**Break-even is under one call, so for a 1-2 command verification container
reuse is a NET LOSS.** It pays only for a long agent step loop, which is exactly
what `ALLOWED_PURPOSES == ("agent_step",)` scopes it to. Recorded as a
measurement and filed, not applied.

### 5. Containment � unchanged, and pinned

Nothing weakened. The workspace mount is still read-write bind-mounted, one
fresh `--rm` container per call, `--pull=never`, `--network none` by default,
and `warm_sandbox` still refuses the verification purpose. `execution/walk_scope.py`
is a **code-walk** authority only.

The fingerprint claim now has the test it never had, **in both directions**:
a source edit (4 files, including a new package) leaves the tag identical and
`ensure_image` the same cost; changing `requirements.txt` MOVES the tag � without
that second direction the first would pass on a fingerprint that ignores
everything.

### 6. Verification actually run (this tree, real Docker 28.5.1, 12 CPUs, 7.6 GiB)

- `python -m pytest tests/test_sandbox.py tests/test_verify.py tests/test_workspace_security.py -q`
  -> **142 passed** (273.13 s).
- `python -m pytest tests/test_ceiling_r2_02_flake.py tests/test_ceiling08_verification.py -q`
  -> **119 passed** (130.76 s).
- `python -m pytest tests/test_ceiling_r2_09_scale.py tests/test_ceiling_r2_10_snapshot_disk.py -q`
  -> **73 passed, 1 skipped** (131.60 s). The skip is the Windows symlink case;
  BLOCKED coverage, not a pass.
- **NEW `python -m pytest execution/test_w1_latency_honesty_pins.py -q`** ->
  **43 passed** (2.5 s), 39 host-only and 4 Docker-gated.
- **P0 pins** `test_containment_pins.py test_flake_gate_pins.py
  test_unsandboxed_pins.py test_verification_honesty_pins.py` + the new file ->
  **97 passed, 1 failed** (56.95 s). **The one failure is the INVERTED PIN that
  is red by design** (`test_RED_BY_DESIGN_no_production_import_path_reaches_the_local_subprocess_sandbox_stub`),
  on `harness/agent_loop.py:782` reaching `harness._stubs` � T1's file, tracked
  as P2.1, and it goes green with no change here.
- `python -m evals.run --check` -> **14/14 ok, verdict CLEAN**, exit 0.
- `python -m ruff check execution` -> **All checks passed!**
- `python -m compileall -q execution` -> exit 0.
- `git diff --check -- execution` -> **exit 0**. The UNSCOPED check exits **2**
  on trailing whitespace in `harness/AGENTS.md` and `runtime/AGENTS.md` �
  **other terminals' uncommitted markdown, not this round's files.**
- `docker info` -> daemon reachable, **28.5.1**, 12 CPUs, 7.62 GiB, overlayfs,
  cgroup v2, 71 images, 0 containers at exit (no residue).
- **No live-provider lane was run** and none is claimed; no credential was
  inspected, requested or retained.

### 7. Not implemented / honest gaps

- **The three skip authorities are still three tables** �
  `harness/skipset.py::SKIP_DIRS` (45), `execution/walk_scope.py::SKIP_DIR_NAMES`
  (49), `memory/code_graph.py::SKIP_DIR_NAMES` (20). They AGREE today and
  `missing_from()` reports `()` in both directions, but nothing enforces it across
  module boundaries. Cross-terminal request filed (T1 asked the same question
  independently, in its own handoff �12.1).
- **`regression_mode="selected"` was never run against a real large suite.**
  Its coverage receipt, its `partial` verdict and its refusal path are all
  proven; its *time saving* on a real repository is not measured, because this
  tree's own suite is not representative of a user's.
- **The `11.3 s` cold graph build is not on the per-call path** � it is paid once
  per unchanged tree and then served from a two-slot digest cache (0.107 s). A
  verification loop that edits a source file every iteration invalidates the
  cache every iteration, so the **cold** number is the one that matters for that
  shape, and it is 10.6 s on a 556-module repository.
- **`pin_image()` still spawns a `docker image inspect` on every call** to feed
  one trace field. Measured at 0.23 s of a ~1.5 s call. NOT memoised: the pin
  exists so a mutable local tag cannot be re-pointed unnoticed, and a
  process-lifetime cache would weaken exactly that. Reported, not taken.
- **Neither `rg` nor `fd` is on this host's PATH**, so every number here is the
  Python-walk arm. `harness/search_engine.py` has the ripgrep path; it did not
  produce these measurements and the claim is not made for it.
- **`execution/warm_sandbox.py` still has no production call site** and still
  carries the AGT-07 containment gap recorded above (no read-only `.git`
  overlay). This round measured it and deliberately did not wire it.
- **The `git diff --numstat` LOC figure is not a measure of this round**, for
  the reason �9 of the P0/W1 entry above records: four terminals share this tree
  and every file under `execution/` already carried other terminals'
  uncommitted work. The exact figures are in the Handoff.

### 8. Cross-terminal requests

1. **T1 + T4 � converge the three skip tables into ONE.** All three now agree
   (`missing_from()` -> `()` both ways), so this is a pure consolidation with no
   behaviour change. The candidate home is a `shared/` module all three import;
   `harness/skipset.py::skip_dirs_for(*, drop=, add=, reason=)` is the variant
   API to preserve, and its `reason` requirement is worth keeping. My four names
   `harness/skipset.py` lacks are `_darcs`, `docs`, `gradle`, `site` � all
   inherited from `memory/code_graph.py`, which is the authority that has been
   indexing with them. **`shared/` is not a file this round may create**, so this
   is filed rather than applied.
2. **T5 � CI selection for two `execution/`-local test modules.**
   `execution/test_w1_latency_honesty_pins.py` (43) and the pre-existing four
   P0 pin modules are inside `execution/`, so `pytest tests/...` never runs them.
   Command: `python -m pytest execution/test_w1_latency_honesty_pins.py
   execution/test_containment_pins.py execution/test_flake_gate_pins.py
   execution/test_unsandboxed_pins.py execution/test_verification_honesty_pins.py -q`
   -> **97 passed, 1 failed by design** (see �6).
3. **T4 / `cli/` � the rail has its numbers now.** Per verification:
   `result.verification_cost` (�3) and `result.regression_check` /
   `regression_coverage`. The rendering rule that matters: **`not_run` must be
   visibly distinct from `False`-as-a-failure AND from `complete`**, and
   `flake_check="not_run"` must be visibly distinct from `not_flaky` � at one
   repetition `flaky` is `False` for both and the three-valued verdict is the
   only thing that tells them apart.
4. **T1 / `harness/core.py` � `regression_mode` is the parameter to pass.**
   `verify()` defaults to `full_suite`, so nothing changes for an existing call
   site. A task that wants the fast inner loop passes
   `regression_mode="selected", changed_files=[...]`; the **final gate should
   keep the default**. Note that `selected` is honest but slow on a COLD graph
   (10.6 s to build, 0.107 s warm), so the saving depends on the digest cache
   hitting between iterations.
5. **`INTERFACES.md` Change Log entry** (not edited; not this round's file).
   NEW `execution/walk_scope.py` (`SKIP_DIR_NAMES`, `SOURCE_SUFFIXES`,
   `WalkResult`, `TRUNCATION_*`, `iter_source_entries`, `walk_python_files`,
   `skip_dir`, `missing_from`, `WALK_ENTRY_BAR`, `WALK_SECONDS_BAR`); two additive
   keyword-only parameters on `verify()` (`regression_mode`, `changed_files`);
   the `REGRESSION_MODES` / `REGRESSION_CHECK_VALUES` closed vocabularies; new
   additive `VerificationResult` attributes (`verification_cost`,
   `regression_check`, `regression_scope`, `regression_coverage`,
   `regression_tests_selected/_total/_skipped`); new additive
   `ExecutionResult` attributes (`elapsed_s`, `container_s`, `overhead_s`,
   `phases`); `TestSelection` gained `coverage_is_complete()`,
   `coverage_is_sound()`, `coverage_receipt()` and a `walk` field. **No
   signature was removed or renamed, no event kind, serialized field, completion
   status, exit code or verifier mint changed, and `shared/types.py` was NOT
   edited.**