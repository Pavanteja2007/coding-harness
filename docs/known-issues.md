# Known issues

The bug corpus. Every entry has a reproducer, so a reader can check the claim
instead of taking it on faith.

**How to read this file:** `open` means it is not fixed. `closed` means it was
found and fixed, and is kept because a bug list that only grows teaches nobody
what was actually hard. Nothing here is inferred — each entry was observed in
the tree, and each carries the command that shows it.

> **The release blocker is SG-01, at the top.** The `daily` engine that 0.3.0
> makes the default scores **0/10** against **10/10** for the legacy engine it
> replaces, on the same 10 real bugs. Measured by the parallel gate in
> [`release-verdict.md`](release-verdict.md), which is the single most
> important document to read before cutting a release.

Report something new via [the bug-report template](../.github/ISSUE_TEMPLATE/bug_report.md).
The three highest-value reports for this project are: a case where the CLI
reported work as **verified** and it was not, a case where it reported work as
verified and a test did not actually run, and a vacuous gate.

---

## Open

### SG-01 — the kernel cannot read the test that defines success

**Severity: critical. THE RELEASE BLOCKER. Measured: the `daily` path scores
0/10 on real bugs; `legacy_agent` scores 10/10.**

The daily kernel hard-denies *reading* a path that matches `protected_paths`.
The protected-path set includes test files, so the agent cannot open the very
test its fix has to satisfy.

Measured on the same 10 real bugs across 5 real open-source projects, by
`docs/release-verdict.md`:

| engine | score |
|---|---|
| `daily` (the 0.3.0 default) | **0 / 10** |
| `legacy_agent` (the 0.2.0 default) | **10 / 10** |

The agent sets out to fix a failing test, is told it may not look at the test,
and stops after one turn. Ten times out of ten.

```bash
python -m scripts.shadow_gate --tasks sh07_packaging_is_normalized_name --arms kernel --profiles default
```

- **File:** `harness/agent_kernel/policy.py:291`
- **Owner:** agent_kernel
- **Why it matters:** the verifier requires the target test to pass. An agent
  that cannot read that test is fixing blind, and the resulting failure looks
  like model incompetence rather than a policy denial.
- **Why a fully green test suite missed it:** neither the 14-task
  prompt-regression matrix nor the 700 Round 2 tests exercise the kernel path
  against real defects. This is the clearest instance in the project of a gate
  that is green and the product broken.
- **Smallest next fix:** distinguish *mutating* a protected path (deny) from
  *reading* one (allow, read-only). The edit policy already reasons about
  intent; the read path should not hard-deny. Then re-run
  `python -m scripts.shadow_gate`, which exits 0 only when the new path is at
  least as correct as the old one.

### SG-05 — a work copy inside your repository is mistaken for your repository

**Severity: high. Ships in 0.3.0.**

The safe-workspace layer walks up and adopts the enclosing project as its
identity, then baselines and protects the *whole* project.

Measured cost on a large repository: **63 minutes before the agent does
anything**. On a toy repository: 0.3 s — which is exactly why no test caught
it. `docs/release-verdict.md` §"the other three blockers" 2.

- **Owner:** `execution/workspace.py`
- **Why it matters:** it is a cliff, not a slope. A user who keeps their repo
  inside a larger project gets a tool that appears hung.

### SG-02 — `knowledge_enabled=False` crashes the run

**Severity: high. Ships in 0.3.0.**

```bash
python -c "import json;from harness import deps;from harness.agent_loop import run_agent;deps.set_call_model(lambda *a, **k: '{\"tool\":\"finish\"}');run_agent('x', '.', {'agent_strategy':'daily','knowledge_enabled':False})"
```

- **Error:** `AttributeError: 'bool' object has no attribute 'compile'`
- **File:** `harness/agent_kernel/strategy.py:1691-1692`
- **Owner:** agent_kernel
- **Why it matters:** it is a *documented, tested configuration* — the OFF arm
  of the knowledge ablation — and it raises. An ablation arm that crashes
  cannot report a number, so this silently removes a measurement from the
  matrix.

### SG-03 — no reachable live provider, so no model-quality evidence exists

**Severity: high for claims, not a code defect.**

`ANTHROPIC_API_KEY` is present (29 characters). TCP to `api.anthropic.com:443`
connects and the TLS 1.3 handshake completes in 0.08 s, so this is **not** a
network block. A minimal `max_tokens=8` call over plain urllib, bypassing
litellm, returns:

```
HTTP 401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}
```

litellm surfaces the same condition as
`InternalServerError: ... [WinError 10061] No connection could be made because
the target machine actively refused it`, which reads like a network outage and
is not one. **That misclassification is worth fixing in its own right**: a
credential problem reported as a socket problem sends an operator to debug the
wrong layer.

- **Owner:** release / environment
- **Reproducer:** the probe in `release-runbook.md` §5
- **Effect:** no live model-quality, latency, or token-spend evidence exists for
  this tree. Nothing in `benchmark.md` claims otherwise.

### SG-04 — the shared worktree is dirty

**Severity: process.**

```bash
git status --short | wc -l     # 368 changed paths at the time of writing
```

- **Owner:** release / process
- **Effect:** no clean candidate SHA, therefore no reproducible build and no
  publishable artifact. Two earlier attempts produced artifact pairs that
  differed because a neighbouring file changed between build A and build B;
  both were discarded.
- **Resolves when:** the tree is quiet and the owner tags.

### A11Y-01 — `TERM=dumb` modal open exceeds the 250 ms gate

**Severity: medium. Measured, not inspected. Contradicts a prior receipt.**

| profile | modal open (wall) | push | paint | result |
|---|---|---|---|---|
| `xterm-256color` | 182.089 ms | 14.213 | 167.877 | 21/21 pass |
| `xterm-256color` + `NO_COLOR=1` | 228.038 ms | 15.736 | 212.302 | 21/21 pass |
| **`TERM=dumb` + reduced motion** | **254.763 ms** | 18.525 | 236.238 | **20/21 — `modal_open_under_250ms` FAILS** |

Over by 4.763 ms, or 1.9%. The paint phase is the cost, not the push.

The terminal-UX round recorded all three profiles as passing. The most
charitable reading is that `TERM=dumb` was always marginal and drifted over; the
reading that would be dishonest is to assume the earlier receipt was right
because it was written down.

- **File:** the paint path in `cli/tui.py` (owner: cli/tui)
- **Reproducer:** `docs/accessibility.md` §4
- **Also note:** `NO_COLOR` at 228.038 ms has 22 ms of headroom on a 250 ms
  gate. It passes today and is the next thing to fail.

### A11Y-02 — no screen-reader announcement path, and the PTY harness is untracked

**Severity: medium. Two distinct defects; see `accessibility.md` §5 for both.**

1. The accessibility prompt for the terminal-UX round asked for *"status
   announcements for screen readers and dumb terminals"*. The dumb-terminal
   half shipped (a plain-text status tooltip, `tests/test_cli_terminal_ux.py`).
   **The screen-reader half has no implementation**: no screen-reader
   detection, no live region, no announcement channel, no config key, no test.
   Yet `logs/terminal-ux/terminal-06.json` records that item as
   `implemented_and_verified`. The claim was true of the dumb-terminal half and
   untrue of the screen-reader half.
2. **The entire real-PTY harness is untracked.** `logs/` is gitignored, and
   `git ls-files` returns **zero** PTY, ConPTY, or `terminal-ux` files. So
   every accessibility receipt in this project — including the one above —
   is reproducible only on the machine that produced it, by a person who still
   has those scripts. A stranger, a CI job, and the next contributor cannot
   run them. A verification harness that is not in version control is not a
   harness.

- **Owner:** cli/tui (the announcement path), repo hygiene (moving the harness
  out of `logs/`)
- **Reproducer:** `git ls-files | grep -c pty` → `0`

### LINT-01 — the lint ratchet is red on three foreign files

**Severity: low for users, blocking for the release gate.**

```bash
python scripts/lint_ratchet.py
```

```
head_iv.py: 1 violation(s) (allowed: 0)
measure_batch_phase.py: 4 violation(s) (allowed: 0)
tests/test_cli_theme.py: 5 violation(s) (allowed: 0)
new lint debt is not allowed - the ratchet only lets counts go down.
```

None of the three is owned by this round. `scripts/lint-baseline.txt` was
deliberately **not** updated: raising a ratchet to make a build green is
exactly what the ratchet exists to prevent. Note that
`tests/test_cli_theme.py` is the colour/contrast/fallback suite — the same
area as A11Y-01, which is worth knowing before declaring the a11y work green.

### DOC-01 — `neo doctor`'s MCP check raises, and a connector has no declared permissions

**Severity: medium. Found by running `doctor` in the session that wrote this
file, on the current tree.**

```bash
python -m cli.main doctor --json
```

`doctor` reports 11 checks, 9 ok, and **2 actionable failures**:

```json
{"key": "mcp_servers", "status": "error",
 "reason": "check raised: ValueError: too many values to unpack (expected 2)"}

{"key": "connector_permissions", "status": "failed",
 "reason": "1 of 1 connector(s) have no declared permissions: ['memory']",
 "remediation": "neo mcp permissions <label> --tool <name> --side-effect ..."}
```

Two distinct defects:

1. **A health check that raises is a broken health check.** A user running
   `doctor` on a broken install gets a Python `ValueError` instead of a
   diagnosis, and the check reports `error` rather than naming what is wrong.
   Whatever the doctor is unpacking has changed shape.
2. **The `memory` connector still declares no permissions.** The extension
   round specified that a connector's blast radius is declared, and `doctor`
   correctly refuses to call it healthy. The remediation is printed — which is
   the feature working — but the declaration itself is still owed.

- **Owner:** cli/doctor.py + extensions
- **Why it is here:** `doctor` is the first command a new user is told to run.
  A health check that crashes on one axis and fails on another is a poor first
  impression, and both findings were one command away from being found.

---

### VER-01 — a stale install makes `neo --version` disagree with `pyproject.toml`

**Severity: low for users, blocking for the release gate. Environmental, not a
source defect.**

```
tests/test_cli_release.py::TestHelpAndVersion::test_version_matches_pyproject
AssertionError: version '0.2.1' does not match pyproject '0.3.0'
```

The test asserts that the installed distribution's version equals
`pyproject.toml`'s — the invariant the 0.3.0 version reconciliation exists to
enforce. It is doing its job.

The cause is a **stale, non-editable 0.2.1 install** whose metadata sits at the
untracked `neo_agent_cli.egg-info/` in the repo root, which is what
`importlib.metadata` reads.

The source is sound, and was proven rather than assumed:

- `python -m build` produced `neo_agent_cli-0.3.0-py3-none-any.whl` and
  `neo_agent_cli-0.3.0.tar.gz`;
- both passed `twine check`;
- the wheel's METADATA reads `Version: 0.3.0`, `License-Expression: MIT`, with
  `neo = cli.main:main` and `harness = cli.main:main` entry points;
- installing that wheel into a fresh throwaway venv reports `neo-agent-cli 0.3.0`.

```bash
python -m pip install -e .     # the fix
```

The same staleness is why `neo capabilities` prints
`neo 0.2.1 (docs describe 0.3.0)` — that probe reporting the mismatch is the
feature working.

---

## Closed, kept for the record

### Release-13 — a source-tree build pair was not reproducible

Two consecutive builds of the *same* dirty tree produced different artifacts.
Root cause: a parallel TUI test process modified untracked `acp/client.py`
between build A and build B. Not packaging nondeterminism — a race. Both pairs
were discarded; reproducibility was later proven from immutable source
snapshots instead. The lesson is now written into the runbook: build twice
only on a quiet tree.

### FakeStore test double drifted from the real store

`tests/test_memory_mcp_release.py`'s `FakeStore.record()` predated the real
store's `repo_path`/`provenance` kwargs, so the test passed against a double
that no longer matched production. The double now mirrors the real signature
deliberately, so future drift fails loudly instead of passing quietly.

### Mock path paid a ~4 s provider import

`runtime/model_capabilities.py` imported `litellm` to be told that a
harness-implemented provider has no metadata. Every eval arm and every offline
test paid ~4 s per model-call class. Fixed with synthetic provider rows; the
mock path now resolves in 0.000 s. Three orchestration timeouts disappeared as
a side effect (4 failing → 7 passing, 79 s → 23.6 s).

---

## What is deliberately not a bug

Stated so nobody files these:

- **A single repetition is the design.** The prompt matrix and the ablation
  both run one rep. It is directional, and `benchmark.md` says so. Making it
  benchmark-grade means paying for many reps of a live provider.
- **The Lint ratchet rejecting new debt** is the ratchet working.
- **`skipped` never counting as `pass`** in the release aggregate and the SLO
  tool is the design, not an oversight.
- **A run with no declared verifier reports `completed_unverified`**, never
  `success`. This is the project's core invariant. It is not a bug that the
  daily path reports it.
