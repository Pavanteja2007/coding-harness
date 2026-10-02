# Release evidence — 0.3.0 source candidate

**Status: NOT RELEASABLE.** Two independent reasons, either of which is
sufficient:

1. **The `daily` default path scores 0/10 on real bugs; the legacy path it
   replaced scores 10/10.** Publishing 0.3.0 as written would ship a downgrade
   to every user. See §0.
2. The tree is dirty (368 changed paths), so there is no candidate SHA, no
   reproducible build, and no publishable artifact.

Nothing has been published, tagged, pushed, or uploaded. This document is the
evidence a reader needs to disagree with that verdict, and the owner's runbook
for changing it.

Machine-readable companions:

- `python -m scripts.release_evidence --json` — the nine release lanes
- `python -m scripts.docs_truth` — version/claim/limit parity
- `python -m evals.run --suite prompt-regression --json` — the prompt matrix

---

## 0. Read this before anything else: the default path is currently worse than what it replaced

`docs/release-verdict.md` — written by a parallel terminal during this round —
ran both agent engines against the same 10 real bugs in 5 real open-source
projects:

| engine | score |
|---|---|
| `daily` (the new 0.3.0 default) | **0 / 10** |
| `legacy_agent` (the 0.2.0 default) | **10 / 10** |

The cause is a single defect, **SG-01**: the kernel's security policy refuses
to let the agent **read** the test file that defines whether the bug is fixed,
because the file matches a protected `tests/` pattern and the refusal is
applied to reads as well as writes. The agent sets out to fix a failing test,
is told it may not look at the test, and stops after one turn. Ten times out
of ten.

This is the single most important fact in this document. The prompt-regression
matrix in §2.1 is 14/14 CLEAN and the Round 2 suites are 700 passing, and
**neither of those exercises the kernel path against real defects** — which is
precisely how a 0/10 result coexisted with a fully green test suite.

**Consequence for the release: 0.3.0's headline feature is its worst feature.**
The version decision in §1 still stands (0.3.0 is the right number for a
breaking change), and everything else in this candidate is sound. But the
default switch must not ship until SG-01 lands. The two options are:

- **Fix SG-01** (apply path protection to writes, not reads) and re-run
  `python -m scripts.shadow_gate`, which exits 0 only when the new path is at
  least as correct as the old one on the shipped configuration.
- **Revert the default** to `legacy_agent` in a 0.2.2 release, land SG-01, then
  ship the default switch in 0.3.0. Slower, and it never exposes a regression
  to users.

`docs/AGENTS.md` records the acceptance criteria. This round did not fix it:
`harness/agent_kernel/policy.py` is not in its ownership.

---

## 1. The version truth, reconciled

There were three different numbers in circulation and none of them agreed.

| surface | said | was true? |
|---|---|---|
| `pyproject.toml` (before this round) | `0.2.1` | Yes — the source tree's declared version. Not what anyone could install. |
| public PyPI | `0.2.0` | Yes — the last artifact that exists. |
| GitHub Releases | *nothing* | Yes — no release has ever been cut. |
| `dist/` in this checkout | `0.2.0` + `0.2.1` pairs | Locally built only. Neither was uploaded. |

So the documentation described `0.2.1` while `pip install neo-agent-cli` gave a
user `0.2.0`. That is the specific defect R2-18 was opened for: a doc that
describes an artifact nobody can install.

**Resolution: the source version is now `0.3.0`, and 0.3.0 is the release the
owner should cut.**

`0.2.1` was never published, so nothing needs to be retracted. It is
*superseded*. The reason it cannot simply be published as-is is that the tree
it would ship contains a **breaking default switch**: an unqualified agent run
now resolves to the `daily` engine instead of `legacy_agent`. That changes the
default engine, the trace event kinds, and the set of files written under
`logs/{task_id}/`. Shipping that as `0.2.0 → 0.2.1` — a patch increment —
would tell every existing user that nothing important changed. It is not a
patch.

`0.2.0` remains the last public release and the only version a stranger can
install today.

---

## 2. Verification lanes: what ran, what did not

Every row below was executed in the session that wrote this file unless it
says otherwise. "Blocked" and "not run" are results. None of them is a pass.

### 2.1 Ran and green

| lane | command | measured result |
|---|---|---|
| Docker availability | `docker version` | **28.5.1** — daemon reachable. |
| Prompt-regression matrix | `python -m evals.run --suite prompt-regression --json` | **CLEAN.** 14 tasks × 8 arms = 112 runs, **98/98 valid comparisons, 0 regressions, 0 errors.** Real Docker sandbox and verifier; only the model reply is scripted. Isolation: `unique_run_root=true`, `source_repositories_unchanged=true`, `one_action_per_scripted_reply=true`. |
| Host self-check | `python -m evals.run --suite prompt-regression --check --json` | **CLEAN, 14/14.** Every task fails pre-fix, goes green on target post-fix, and leaves the suite green. |
| Round 2 regression suites | `pytest tests/test_ceiling_r2_*.py` (14 files) | **700 passed, 2 skipped.** |
| ACP protocol adapter | `pytest tests/test_acp.py` | **29 passed** in 2.05 s. Real stdio and in-memory transports. |
| Routing-capability ablation | `python -m evals.routing_capability` | **CLEAN**, 21 checks, 0 regressions. Offline mock provider; the router, capability screen, price ladder, and cost report are the shipping code. |
| Difficulty holdout (negative result) | `python -m evals.difficulty_holdout` | Ran and **declined to ship.** See `docs/benchmark.md` §5. |
| SLO measurement | `python -m evals.slos --logs-root logs` | **INSUFFICIENT_EVIDENCE** — 44 measurements accepted, all nine objectives unmeasured. Correct: this checkout has no live-run evidence for them. |
| CI-truthfulness gate | `python -m evals.ci_truth --check` | **CI_TRUTHFUL** — all six checks. |
| Documentation truth gate | `python -m scripts.docs_truth` | See §5 — **fails on one surface this round does not own.** |

### 2.2 Ran, and found a real failure

**Terminal accessibility under a real attached pseudo-terminal.** Executed via
`script -qec` inside WSL, which allocates a genuine PTY so the child's stdout
and stderr are real TTYs. (Run through a pipe instead, the probe correctly
refuses to report `passed: true` — which is exactly what it did on the first
attempt, and why the PTY is allocated explicitly.)

| profile | result | modal open (wall / push / paint) |
|---|---|---|
| `xterm-256color`, default theme | **21/21 pass** | 182.089 / 14.213 / 167.877 ms |
| `xterm-256color`, `NO_COLOR=1` | **21/21 pass** | 228.038 / 15.736 / 212.302 ms — only 22 ms of headroom |
| **`TERM=dumb` + reduced motion** | **20/21 — `modal_open_under_250ms` FAILS** | **254.763 / 18.525 / 236.238 ms** — over by 4.763 ms |

Performance across the green profiles: event-to-UI p95 44.965–46.792 ms (gate
250 ms), input-ack p95 0.555–49.954 ms (gate 100 ms), UI-thread stalls over
500 ms: **0** in every profile and phase.

This **contradicts** the terminal-UX round's own handoff, which recorded all
three profiles as passing. The honest reading is that `TERM=dumb` is marginal
and drifted over the line, not that the earlier receipt was fabricated: the
margin is 1.9%. Details and the exact repro command: `docs/accessibility.md`.

### 2.3 Blocked — the lane could not run

| lane | blocking layer | evidence |
|---|---|---|
| **Live model provider** | **Credential rejected.** | `ANTHROPIC_API_KEY` is present (29 chars). TCP to `api.anthropic.com:443` connects, and the TLS 1.3 handshake completes in 0.08 s — so this is *not* a network block. A minimal `max_tokens=8` call over plain urllib, bypassing litellm, returns **`HTTP 401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}`**. litellm surfaces the same as `InternalServerError ... [WinError 10061]`, which reads like a network fault and is not one. **No live model-quality evidence exists for this tree, and none is claimed anywhere.** |
| **Native Windows ConPTY** | **Session has no interactive window station.** | `python logs/terminal-ux/terminal09_conpty_check.py` → `{"available": false, "reason": "CreatePseudoConsole returned FALSE with last error 0"}`, exit 1. The driver correctly distinguishes "API missing" from "API present but unusable here" and reports the latter as blocked rather than passing. The WSL POSIX PTY is the real-terminal evidence instead. |
| **Human manual-repair sample** | **No explicit observations.** | `logs/oss-round6/multi_repo_report.json` holds three real-OSS runs (`parse`, `bottle`, `click`) through the real scheduler/worker/harness/Docker with a scripted model. **None carries a human manual-repair boolean.** The daily-driver loader requires one and fails closed, so the lane is blocked rather than inferred from the fact that the runs succeeded. |

### 2.4 Not run, and why

| lane | why not |
|---|---|
| **Full repository test suite** | Three other terminals are running suites in this shared worktree right now. A full-suite result against a moving tree is not evidence — the project's own stated standard is that this gate runs "stable tree, nothing else running." Running it now would also sabotage the three parallel lanes. The Round 2 selection (700 passed) and the 14 × 8 prompt matrix are reported instead, and the full suite is listed here as **not run**. |
| **Two-build reproducibility** | The tree is dirty: **368 changed paths** and no candidate SHA. Building twice from a source that other terminals are editing produces a "nondeterminism" that is really just a race, and a mixed artifact is worse than no artifact. Prior rounds did prove reproducibility from *immutable* source snapshots; that evidence is in `scripts/AGENTS.md` and is **not** a claim about this tree. |
| **Clean-room install matrix** | Requires a candidate artifact built from a clean tree. Blocked by the same 368-path dirty state. |
| **SBOM + vulnerability scan against a candidate** | `vulnerability_scan` runs against `pyproject.toml` and needs no artifact; the `sbom` lane does. Not run. |
| **Lint ratchet** | **Ran, and is RED.** New debt in 3 files: `head_iv.py` (1), `measure_batch_phase.py` (4), `tests/test_cli_theme.py` (5). None of the three is owned by this round. `scripts/lint-baseline.txt` was deliberately **not** updated — raising a ratchet to make a build green is the failure mode the ratchet exists to prevent. |

---

## 3. Known blockers shipping with 0.3.0

These are open, have reproducers, and are not fixed. Full detail in
`docs/known-issues.md`.

| ID | blocker | owner | ships in 0.3.0? |
|---|---|---|---|
| **SG-01** | The kernel hard-denies *reading* a path that matches `protected_paths`, so the agent cannot read the test that defines success. **Measured: the `daily` path scores 0/10 on real bugs; legacy scores 10/10.** `harness/agent_kernel/policy.py:291` | agent_kernel | **Yes — and this is the release blocker.** See §0. |
| **SG-02** | The documented `knowledge_enabled=False` OFF arm crashes: `AttributeError: 'bool' object has no attribute 'compile'`. `harness/agent_kernel/strategy.py:1691` | agent_kernel | **Yes.** |
| **SG-05** | The safe-workspace layer adopts the *enclosing* project when a work copy lives inside your repository, then baselines and protects your whole project. **Measured: 63 minutes before the agent does anything on a large repo; 0.3 s on a toy repo, which is why no test caught it.** | execution/workspace | **Yes.** |
| **SG-03** | No reachable live provider, so no model-quality evidence exists. **Root cause corrected by this round:** the recorded cause was "provider capacity / no available channel"; the measured cause is a rejected credential (HTTP 401). | release / environment | Yes, as a documentation gap. |
| **SG-04** | The shared worktree is dirty, so there is no clean candidate SHA and no reproducible build. `git status --short` | release / process | Resolved by the owner at tag time. |
| **A11Y-01** | `TERM=dumb` modal open is 254.763 ms against a 250 ms gate. | cli / tui | Yes. Measured this round. |
| **A11Y-02** | No screen-reader announcement path exists, and the entire real-PTY harness is untracked (`logs/` is gitignored), so no stranger or CI job can reproduce any a11y receipt. | cli / tui + repo hygiene | Yes. |
| **DOC-01** | `neo doctor`'s MCP check raises `ValueError: too many values to unpack`, and the `memory` connector declares no permissions. | cli/doctor + extensions | Yes. Found this round. |
| **VER-01** | A stale non-editable 0.2.1 install makes `neo --version` disagree with `pyproject.toml` until `pip install -e .` runs. | environment | Resolved by a reinstall. |
| **LINT-01** | Lint ratchet red on 3 foreign files. | owners of those files | Yes, until fixed. |

---

## 4. Publishing is owner-only, and this round did not do it

`python -m scripts.release_evidence` cannot publish. It has no uploader: the
publish gate needs both a green report and an explicit human phrase
(`NEO_RELEASE_APPROVED=publish`), and the module never shells out to twine.
A bare `1` is not consent to ship.

**Actions taken by this round: none.** No commit, no tag, no push, no upload,
no publish, no release. No `git reset`, `clean`, `checkout --`, `restore`,
`stash`, or `rebase`. The only file writes were to documentation, `pyproject.toml`,
and `.github/`.

---

## 5. One gate is red, and it is this round's fault

`python -m scripts.docs_truth` now fails on exactly one finding:

```
[versions] site/src/lib/content/releases.ts: declares v0.2.1 but pyproject declares 0.3.0
```

`site/src/lib/content/releases.ts` is the marketing site's release list. It is
a transcription of `CHANGELOG.md` by its own header comment, and
`python -m scripts.docs_truth --gate site` is a required lane in
`.github/workflows/release-gate.yml`. It is **not** in this round's declared
file ownership, so it was not edited.

**The fix is one entry**, and it is specified exactly in
`docs/AGENTS.md` § "Cross-terminal requests". The gate was left red rather
than satisfied by reverting the version to 0.2.1: a green gate that hides a
stale public claim is precisely the defect this project exists to prevent.

---

## 6. Owner runbook

Full step-by-step, with every precondition, in
[`release-runbook.md`](release-runbook.md). The short version:

1. Land every Round 2 lane, including R2-18's documentation. Get a quiet tree.
2. Close SG-01 and SG-02, or accept them in writing in the release note.
3. Fix the site release list so `docs_truth --gate site` is green.
4. `git status --short` must be empty. That closes SG-04.
5. `python -m evals.run --check`, then the full matrix, then the full suite on
   the quiet tree.
6. `python -m scripts.release_evidence --dist dist --json` — all nine lanes
   green, or name the ones that are not.
7. `python -m scripts.verify_release --dist dist --require-clean --require-tag v0.3.0`
8. `python -m twine check dist/*` and `python -m twine upload` with the two
   **exact** filenames. Never a wildcard.
9. Cut the GitHub release from the tag, pasting the changelog's lane table
   unchanged.
