# HANDOFF.md — Neo: final ceiling gate (updated 2026-09-27; release candidate 0.2.1)

> **THE VERDICT IS `blocked`, NOT `cutover`.** The final gate ran against the
> current tree. On **5 real OSS repositories and 10 real defects**, the shipped
> kernel path scored **0/10** while the legacy compatibility loop it replaced
> scored **10/10** — every kernel run is refused on its **first `read` of the
> test that defines success**. The daily-driver matrix is **11/11 CLEAN** and
> the prompt-regression matrix is **14/14 CLEAN**, so the product is not broken:
> the **default product path is currently weaker than the compatibility path**,
> which is exactly the condition a cutover gate exists to catch.
>
> Evidence: `logs/ceiling/final-gate.json`, `logs/ceiling/terminal-17.json`,
> `logs/ceiling/shadow-shadow_report.json`,
> `logs/ceiling/daily-driver-report.json`, `logs/ceiling/evidence-report.json`.
> Reproduce with `python -m scripts.shadow_gate`.

## What is blocked, and who owns it

| id | owner | file | what is wrong |
|---|---|---|---|
| **SG-01** | harness/agent_kernel (T01 + T05) | `harness/agent_kernel/policy.py:291`, `:73-84` | the hard-deny path check fires on **read-only** calls too, and the default `protected_paths` include `test_*.py` / `tests/*`. A `read` of the target test is a terminal deny (`secret path refused: tests/test_utils.py`). The legacy loop applies that protection only to EDIT/WRITE, which is why it can still read the test. **This is the whole 0/10.** |
| **SG-02** | harness/agent_kernel (T05) | `harness/agent_kernel/strategy.py:1691-1692`, `:1745-1748` | the documented `knowledge_enabled=False` OFF arm **crashes** the run: the memo sentinel is the boolean `False` and the guard is `is not None`, so `_compile_knowledge` calls `False.compile()`. Captured with a full traceback. |
| **SG-05** | execution (T02 / R2) | `execution/workspace.py:1249` | a `.git`-less work copy inside a git repository is **adopted into the enclosing repository's identity**, so the safe workspace baselines the user's whole project. Measured cost: **3816 s (63.6 min) before the first model call** on this 291k-file tree; 0.29 s in a 2-file repo, which is why no test-sized repro saw it. `logs/{task}/work/` is a `.git`-less snapshot by design, so this is the real shape whenever the log root is inside the user's repo. |
| **SG-03** | environment | — | **no live provider is reachable.** The ambient router credential authenticates and its model list resolves to one model, but 3/3 completions return `ServiceUnavailableError: no available channel`. Every model interaction in this gate is a deterministic scripted boundary, so **no model-quality claim is made anywhere in this round's artifacts.** |
| **SG-04** | release / process | `HANDOFF.md`, `CHANGELOG.md` | the shared tree is dirty (`6287cb1`, dirty). **No clean candidate SHA, no reproducible build, no clean-room install, no tag.** Committing and tagging need explicit human approval, which this gate does not have and does not assume. |

Each has a one-line reproduction in `logs/ceiling/terminal-17.json`. **No
`NOT_READY` was converted to `READY` by documentation**, and no git write,
upload, or publish was performed.

## What the gate actually measured

**Phase 1 — shadow mode.** Ten tasks over five real repositories
(`r1chardj0n3s/parse`, `jaraco/path`, `bottlepy/bottle`, `pypa/packaging`,
`pallets/click`), each a single-site defect with a **real upstream test** as
its oracle in the **real Docker sandbox**, plus an **independent behavior
probe** that asserts observable library behavior rather than the literal fix.
Both arms received the identical config except the strategy. A task whose
pristine suite is not green, or whose defect the real suite does not detect, is
**blocked — never a pass**. 40 runs, two profiles:

| profile / arm | correctness | verified | turns | p50 | p95 | policy refusals |
|---|---|---|---|---|---|---|
| **shipped / legacy** | **10/10** | 10/10 | 6.0 | 47.1 s | 142.1 s | 0 |
| **shipped / kernel** | **0/10** | 0/10 | 1.0 | 31.7 s | 81.2 s | **10** |
| comparison / legacy | 10/10 | 10/10 | 6.0 | 46.0 s | 139.2 s | 0 |
| comparison / kernel | 8/10 | 8/10 | 6.0 | 72.6 s | 153.0 s | 0 |

The `comparison` profile narrows exactly one documented key
(`protected_paths`) so the two paths are measurable **at all**; it is
diagnostic and never feeds the verdict. Its two kernel failures (bottle
`DictProperty` and bottle route binding) are reported as measurement, not
diagnosed.

**Workspace safety: PASS.** The reference clone's content digest — computed
independently of git, so a mutation without a git change is still caught — and
`git status --porcelain` were byte-identical before and after all 40 runs.

**Phase 2 — adversarial daily driver: 11/11 CLEAN.** Long session with resume
(verified run, retained conversation, turn ledger, turn-1 observation still in
the journal the later request was built from); mid-run steering (inject → a
**second** buffer instance sees it → consumed at a named checkpoint); stale
edit (a 64-zero digest is refused, the file is byte-unchanged); malformed tool
call (invalid JSON / empty / unknown tool each become a recoverable marker);
provider outage (a real litellm `ServiceUnavailableError` retries and recovers;
a real `AuthenticationError` is terminal and is **not** retried); untrusted
issue content (three injection payloads quarantined, canary never surfaced);
detached run and attach (replay: 4 events, 0 gaps); cross-repo session search
(a foreign row is found unfiltered and excluded when a repo is given);
read-only review (tree digest and git status unchanged with a real pending
change); headless/TUI parity (`completed_unverified` is never success and never
exit 0); bounded context (5,000 messages measure flat, under the 0.8 target).

**Phase 3 — release evidence.** `prompt_regression_check` **pass**
(`python -m evals.run --suite prompt-regression --check` → 14/14 ok, CLEAN).
**Skipped, never passed:** `live_provider_quality` (SG-03), `full_test_suite`
(not selected in this invocation), `release_evidence_aggregate` (no candidate
artifact, SG-04). `docs_truth` and `ci_truth` are recorded as **declared**
(owned by T16 / T15) rather than re-asserted as this gate's own result.

**Phase 4 — verdict.** `blocked`. Derived, never chosen: `cutover` requires
≥5 repos, ≥10 tasks, zero workspace mutations, kernel ≥ legacy **on the shipped
profile**, a CLEAN daily matrix, no failing lane and zero blockers.

## The five invariants

| invariant | state |
|---|---|
| the verifier mints verified success | **HELD** — the mint condition is still in `harness/core.py`, and each of the legacy arm's 10 verified successes carries an independent post-run Docker verification *and* a behavior probe |
| `completed_unverified` never renders as success | **HELD** — `cli.runview` projects it to `completed_unverified`; the headless envelope is neither success nor exit 0 |
| the TUI consumes the event journal | **HELD** — `cli/tui.py` reads the run projection and `trace.jsonl`; the headless parity scenario reads the same projection |
| the original repository is not mutated by a verified run | **HELD** — 40/40 runs, git-independent content digest |
| **the daily path is the strongest verified path** | **MEASURED FALSE** — 0/10 vs 10/10, because of SG-01. This is the one invariant the gate found broken, and it is why the verdict is `blocked` rather than `shadow_only`. |

## What a future session must know

- **The gate is a shipped tool, not a one-off script.** `scripts/shadow_gate.py`
  is the four-phase driver; `tests/test_ceiling17_shadow_gate.py` (39 tests) is
  the suite that tries to make the gate report better than the evidence and
  asserts it refuses: a skipped lane is not a pass, a missing metric is not a
  pass, a defect that must occur exactly once does, and the two arms are proven
  to differ in **exactly** `agent_strategy`.
- **The ten tasks are validated, not asserted.** Every one was checked to
  PASS its behavior probe on pristine upstream source and FAIL it with the
  defect applied. Four of the ten needed their target test re-selected after the
  first attempt proved that test did not actually cover the defect.
- **The verification honest-check that is now automated:** the gate reads
  `raw_output` (not `output`) from a `VerificationResult`, quotes a `-k`
  expression for the sandbox shell, and resolves every run path to absolute —
  the last one because a relative run root made the probes grade the
  **installed** library instead of the working copy, which is a silent false
  pass. Do not reintroduce any of the three.
- **`python -m scripts.shadow_gate` exits 2 whenever the verdict is not
  `cutover`.** It is a release gate, not a report generator.
- **`--out-root` defaults OUTSIDE every repository** on purpose. Putting a
  `.git`-less work copy inside a repository is SG-05's reproducer; the default
  placement is also the product's own post-T03 layout.

## Honest boundaries

- The shadow comparison is a **PATH** comparison, not a model-quality
  comparison. A scripted model applies the same edit on both arms, so the delta
  measured is the loop, the policy, the tool protocol and the verifier — which
  is what a cutover decision is about. With no live provider (SG-03), no
  quality claim is available at all.
- Each task's defect is a **synthesized** single-site change in real upstream
  source, not a historical upstream bug report. The oracle and the probe are
  real; the defect is constructed.
- The full test suite, reproducible build, and clean-room install were **not
  run** by this gate and are reported as **skipped**, not as passes. Other
  terminals have recorded partial results; none is aggregated here as if it were
  a whole-suite result.
- `SG-02` and `SG-05` were **not patched**. Both are in another module's
  ownership and both are safety boundaries: `knowledge_enabled=False` is a
  configuration contract and the workspace identity decides **what every other
  module protects**. A gate reports; it does not unilaterally widen a deny rule
  or re-root a safety boundary to make its own number look better.

## Commit policy

By design, **nothing was committed during the ceiling rounds** (many parallel
terminals, one working tree) and **nothing was committed by this gate**. There
is no clean candidate SHA (SG-04), so there is no tag and no artifact to
publish. Committing, tagging, uploading, and publishing each require explicit
human approval, which this gate does not have and does not assume.
