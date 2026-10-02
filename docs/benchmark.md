# Neo benchmark

**What this is:** every number Neo has actually measured, on task sets whose
composition is stated, with the failures included.

**What this is not:** an SWE-bench result. No SWE-bench number is claimed
anywhere in this project, and none appears below. Read §2 before quoting
anything here.

> ### Before you read a single number: the default engine scores 0/10
>
> Every success figure below was produced by the **`legacy_agent`** path, which
> is the 0.2.0 default. A parallel gate ([`release-verdict.md`](release-verdict.md))
> measured the **`daily` engine** — the one 0.3.0 makes the default — against
> the same 10 real bugs in 5 real open-source projects and scored it
> **0/10, against 10/10 for legacy**. One cause: the kernel's policy refuses
> to let the agent read the test that defines success.
>
> So these numbers describe the working engine. They do **not** describe what
> an unqualified `neo fix` will do after 0.3.0 ships. Nothing here should be
> read as evidence that the 0.3.0 default is ready.

Every figure below was re-read from the artifact on disk in the session that
wrote this file. Nothing is quoted from memory or from prose elsewhere in the
repository.

---

## 1. The one-paragraph honest summary

Neo's verifier-gated fix loop works reliably on small, well-specified Python
defects — 14/14 across 8 prompt configurations and 98/98 valid comparisons
through a real Docker sandbox. On **real third-party repositories the
success rate is 40–60%**, and that is the number that matters, because it is
the only one measured on code nobody here wrote. The cost mechanism (adaptive
model routing) is 2.6–4.2× cheaper than always-expensive routing, at equal or
better success, but every cost figure uses **proxy price rates**, not billing.
There is **no live-provider evidence at all**: the credential available to this
session is rejected with HTTP 401, so nothing here measures model quality,
real latency, or real token spend. One significant improvement — a structural
difficulty predictor — was measured, beat its predecessor, and was
**deliberately not shipped** because the held-out split was too small to
justify it. That negative result is in §5 and is the most useful result here.

---

## 2. Methodology, and why these numbers are weak

Read this before quoting §3 or §4. Every limitation below is a real reason the
numbers would move on a different machine, a different provider, or a different
day.

| property | value | consequence |
|---|---|---|
| Repetitions per task | **1** | No confidence intervals. A 5-task set has a 95% CI of roughly ±44 points. Success-rate *differences* of 1–2 tasks are noise. |
| Task-set size | 5, 14, or 16 | Small-n. Directional only. |
| Model replies | **Scripted / deterministic** for every task set except where noted | These measure the **machinery** — the verifier, the gates, the routing policy, the receipts. They do **not** measure whether a real model can write a correct patch. This is the single largest caveat in the document. |
| Cost model | **Proxy price rates** | Free-tier endpoints report zero cost. Every cost figure is a *price-model delta*, not money spent. |
| Bugs | **Introduced by the project**, not collected | Each is a single deliberate edit, self-checked to fail pre-fix and pass post-fix. Real bug corpora contain bugs nobody reverse-engineered. |
| Docker | Real sandbox, real verifier | This part is genuine, not mocked. |
| Live provider | **Never reached** | See §6. |

`runtime/ablation.py` states this about its own numbers, and the statement has
not been softened: *"Round 3: 16 tasks x 1 rep — bigger than Round 2's 5, still
not benchmark-grade: success-rate deltas remain directional only."*

---

## 3. Task set A — the prompt-regression matrix (14 tasks)

**The most trustworthy number in this document**, because it is the only one
that is deterministic, reproducible, and re-runnable by a stranger on a clean
checkout with nothing but Docker.

```
python -m evals.run --suite prompt-regression --json
```

| | |
|---|---|
| Tasks | 14 |
| Arms | 8 (`baseline`, `no_memory`, `no_lint`, `no_docs`, `no_agent_tests`, `no_webfetch`, `no_skills`, `pre_round`) |
| Runs | 112 |
| Verdict | **CLEAN** |
| Valid comparisons | **98 / 98** |
| Regressions | **0** |
| Errors | **0** |
| Per-arm success | **14/14 on all eight arms** (rate 1.0) |
| Isolation | `unique_run_root=true`, `source_repositories_unchanged=true`, `one_action_per_scripted_reply=true` |
| Docker | real (28.5.1) |

The eight arms each disable one improvement and keep everything else, so
`pre_round` is the wholesale "turn this round off" control. **Zero
regressions** means removing any single prompt feature — or all of them at
once — did not break a single task. That is a statement about *not regressing*,
which is a weaker and more appropriate claim than "these features help".

### Per-task detail

| task | class | arms ok | attempts | notes |
|---|---|---|---|---|
| `bug01_wrap` | fixture (committed repo) | 8/8 | 1 | cross-project comparability set |
| `bug02_mean` | fixture | 8/8 | 1 | |
| `bug03_stack` | fixture | 8/8 | 1 | |
| `bug04_nameerror` | fixture | 8/8 | 1 | the bug *is* an F821 undefined name |
| `bug05_cart` | fixture | 8/8 | 1 | |
| `eval_strip_boundary` | synthesized | 8/8 | 1 | string-boundary class (`lstrip("www.")`) |
| `eval_wrong_operator` | synthesized | 8/8 | 1 | `day == 6 and day == 7` |
| `eval_wrong_constant` | synthesized | 8/8 | 1 | `min(60 * 2**attempt, 60)` |
| `eval_lost_guard` | synthesized | 8/8 | 1 | lost exception guard |
| `eval_repair_retry` | scenario | 8/8 | **2** | requires the REPAIR loop; `expects_retry=True` |
| `eval_docs_lookup` | scenario | 8/8 | 1 | requires a `docs_lookup` receipt |
| `eval_lint_undefined` | scenario | 8/8 | **2** | lint gate must fire; `expects_retry=True` |
| `eval_fetch_webpage` | scenario | 8/8 | 1 | real network fetch to `pypi.org` |
| `eval_skills_injection` | scenario | 8/8 | 1 | skills scan + inject |

Wall time per run was 12.7–22.4 s. The two 2-attempt tasks are the ones where a
first attempt is *supposed* to fail and the repair loop is supposed to recover
— a 1-attempt result there would be a failure, and the matrix scores for it.

The host self-check (`--check`, no Docker, ~30 s) independently validates that
each task fails pre-fix, goes green on target post-fix, and leaves the suite
green: **14/14, 0 failures, 0 skips**.

---

## 4. Task set B — real OSS repositories (5 repos)

**This is the honest headline and it is not 100%.**

```
python -m runtime.ablation --tasks multirepo --arm off --arm on
```

Five real third-party repositories at pinned SHAs — more-itertools
(`ca711220a6`), arrow (`2224255c4a`), inflect (`262a247d2d`), semver
(`6adf8765f6` = v3.0.4), boltons (`961dcff3f4`) — each with one deliberately
introduced bug, upstream code otherwise. The bugs are ours; the surrounding
code is not, and it is unfamiliar code in unfamiliar layouts.

| run | always-expensive (OFF) | adaptive (ON) | cost ratio OFF/ON | escalations |
|---|---|---|---|---|
| v6-multirepo | **2/5 (40%)** | **3/5 (60%)** | **4.19×** | 0 |

Tokens: ON used 26,637 fewer than OFF. Cost delta: −$0.2329.

**So: on real third-party code, the system solves 2 or 3 out of 5, and the
adaptive arm solved one more than the control at 24% of the cost.** Two
things follow, and both should be said out loud:

1. The 3-vs-2 difference is **one task**. With n=5 and one repetition it is
   not statistically distinguishable from a coin flip. The *cost* difference is
   large and consistent; the *success* difference is not established.
2. 40% and 60% are the real numbers. Any claim of "100% success" in this
   project refers to fixture and synthesized tasks, and must say so.

### Why the cheap arm does better here

From the project's own record, and offered as a hypothesis rather than a
result: the cheap tier's median latency (p50 22 s) versus the expensive tier's
p95 (309 s) meant more fix attempts fit inside the same wall-clock budget, so
the adaptive arm got 3 attempts where the control effectively got fewer. That
is a **budget** effect, not a model-quality effect, and the project does not
claim the routing mechanism chose better.

### The honest negative result, kept

`v1-heuristic`, the first version of the routing redesign, is retained in the
record because it **failed**: OFF 0/5 at $0.0241, ON 3/5 at $0.0467 — the
adaptive arm cost **more** and the mechanism was actively harmful. It is in
`RESULTS.md` rather than deleted. A results file with no failures in it is a
marketing document.

---

## 5. A measured improvement that was deliberately NOT shipped

`python -m evals.difficulty_holdout` compares the incumbent lexical difficulty
predictor against a structural one on a held-out split, with a declared
minimum sample floor.

| | legacy | structural |
|---|---|---|
| Train (n=90) easy/hard accuracy | 0.9000 | 0.9222 |
| **Holdout (n=17) easy/hard accuracy** | **0.8235** | **0.9412** |
| Holdout missed escalations | 1 | 1 |
| Holdout false escalations | 2 | **0** |
| Holdout mean ordinal error | 0.5294 | **0.2353** |

The structural predictor won on the held-out groups. It was still **not
shipped**, and the tool's own recorded reasoning is the right one:

> "the structural predictor beat the incumbent on the held-out groups, but
> that split carries only 1 hard-labelled observation against a declared floor
> of 3. A win on that few hard cases is not evidence enough to replace a
> shipped predictor, so it is NOT shipped: the incumbent stays the default and
> the result is recorded as promising-but-unproven."

`verdict.ship: false`. `verdict.sample_adequate: false`. Coverage: 271
accepted records, 107 rows, all features resolved; `fan_in` was measured on
**0** rows, so one declared feature contributed nothing. The incumbent
predictor remains the default.

This is the result we would point at first. An agent harness that reports only
the wins has a selection problem; one that publishes "we built a better thing
and declined to ship it because n=1" is telling you what its numbers are
worth.

---

## 6. What is NOT measured

Stated as a list, because an unstated gap reads as a pass.

| not measured | why | consequence |
|---|---|---|
| **Live-provider model quality** | Credential rejected: `HTTP 401 authentication_error`. TLS to `api.anthropic.com` completes in 0.08 s, so it is the credential, not the network. | **No claim about any model's coding ability.** Every number above is scripted-model machinery evidence. |
| **Real latency and real token spend** | Same | Real cost is unknown; §4's cost is a price model. |
| **SWE-bench (Lite or full)** | Deferred by the project spec; never implemented | No headline number exists. Do not infer one from §3/§4. |
| **The 20-task × 2-provider live quality matrix** | `python -m evals.live_quality` exists and is fully implemented, but it needs working credentials. It returns `blocked` and claims nothing. | — |
| **Nine service-level objectives** | `python -m evals.slos` returns **`INSUFFICIENT_EVIDENCE`**: 44 measurements accepted, all nine objectives unmeasured. | The tool reports the absence rather than zero. That is correct behaviour and is not a pass. |
| **Human manual-repair rate** | Three real-OSS runs exist (`parse`, `bottle`, `click`; real Docker, scripted model, all succeeded) but **none carries an explicit human manual-repair boolean**, so the lane fails closed. | "Did a human have to hand-fix the agent's output?" is unanswered. |
| **The daily-driver 26-case × 2-arm matrix** | Exists and is implemented. Not re-run in this round. | Its own last recorded verdict is `NOT_READY`. |
| **Full repository test suite** | Not run: three other terminals are running suites in this shared tree. | See `release-evidence.md` §2.4. |

---

## 7. Failures, collected

The point of this section is that a benchmark reporting only successes is
marketing. Every open failure with a reproducer is in
[`known-issues.md`](known-issues.md). The four that constrain what a user can
claim:

| ID | failure | effect on the numbers above |
|---|---|---|
| **SG-01** | The kernel hard-denies *reading* a path matching `protected_paths`, so the `daily` default cannot read the test that defines success (`harness/agent_kernel/policy.py:291`) | The new default engine has a known defect on the path users are being moved onto. |
| **SG-02** | `knowledge_enabled=False` crashes: `AttributeError: 'bool' object has no attribute 'compile'` (`harness/agent_kernel/strategy.py:1691`) | One documented configuration is broken. |
| **A11Y-01** | `TERM=dumb` modal open measured **254.763 ms** against a 250 ms gate | A real-terminal accessibility gate regressed; the previous round recorded it as passing. |
| **R2-13** | Structural predictor won but was **not shipped** (§5) | A real improvement is deliberately left on the shelf. That is a decision, not a defect. |

Plus, from the Round 2 selection: 2 skipped tests across 14 files. Skips are
listed in the CI artifacts rather than counted as passes.

And the one that dominates all of §3 and §4:

| ID | failure | effect on the numbers above |
|---|---|---|
| **SG-01** | The kernel hard-denies *reading* a path matching `protected_paths`, so the `daily` default cannot read the test that defines success. **Measured 0/10 vs legacy 10/10 on the same 10 real bugs.** `harness/agent_kernel/policy.py:291` | **Every success rate in this document was measured on the engine 0.3.0 is about to stop using by default.** |

This is also the sharpest available lesson about gates: the prompt-regression
matrix in §3 is 14/14 CLEAN, 98/98 comparisons, and the Round 2 suites are 700
passing — and the default path still scores zero. **A green suite and a broken
product coexisted here for an entire round**, because no gate in the tree
exercised the kernel against real defects.

---

## 8. Reproducing any of this

```bash
# Task set A, host self-check, no Docker, ~30 s
python -m evals.run --check

# Task set A, full matrix, real Docker, ~15 min
python -m evals.run --suite prompt-regression --json

# Task set B, real OSS repos, needs provider credentials
python -m runtime.ablation --tasks multirepo --arm off --arm on

# The negative result, offline
python -m evals.difficulty_holdout --json

# Routing capability, offline
python -m evals.routing_capability --json

# SLO honesty check
python -m evals.slos --logs-root logs --json
```

Reports land in `logs/evals/<run-id>/` and `logs/ablations/<ts>/`. Both
directories are gitignored, so a clean clone has no historical reports to read
— the numbers above are transcribed from artifacts that were present at the
time of writing, with their paths named so they can be re-derived on a machine
that still has them.
