# G3a — the P3a memory gate, published

**Verdict: `G3A_RED`** — 14 pass / 0 fail / **1 blocked** / 0 not_implemented.

The red is not a defect in this wave. It is `r11_live_provider_lane`, which is
`blocked` because no provider is reachable from this checkout, and this gate
counts a blocking `blocked` as RED on purpose: the lane that could not run is
precisely the lane that would have falsified a quality claim.

```powershell
python -m evals.gates.P3            # the verdict, exit 2 when red
python -m evals.gates.P3 --prove    # also runs the named proving-test FILES
python -m evals.gates.P3 --selftest # proves the gate can fail
```

## What was measured, and by what

| rung | claim | how it was measured |
|---|---|---|
| r1 | recall works | `recall@5 = 1.0` over 5 labelled queries through the real FTS5 trigram engine |
| r2 | the ablation has an arm per axis | 5 arms, each declaring its FULL key set |
| r3 | no memory key is in `DEFAULTS` | all 3 keys absent, read by key presence AND truthiness |
| r4 | unavailable ≠ empty | SQLite 3.40.1 FTS5/unicode61; every outcome names its engine |
| r5 | a mid-session write costs zero cache | prefix digest byte-identical across a mid-session write |
| r6 | nothing persists without a human | real `propose` → 0 store rows; operator `apply` → 2 |
| r7 | revocation is immediate and audited | revoked row gone from `search`/`get`/`count`/`list_for_repo` |
| r8 | a cap reports what it dropped | 160-char cap named 5 dropped rows and 470 dropped chars |
| r9 | a user can leave | both export formats, including the tombstone and its reason |
| r10 | two-phase pipeline | credential redacted before the transcript reached the job row |
| **r11** | **a live-provider measurement** | **BLOCKED — no provider reachable** |
| **r12** | **a Docker-backed memory measurement** | **2 lanes × 14 tasks × 3 reps × 2 arms = 168 Docker-verified runs** |
| **r13** | **the inbox gates every write path** | **5/5 inbox rungs, AST-enumerated** |
| **r14** | **the corpus is good enough to use** | **precision, staleness, retraction, harm** |
| **r15** | **the ablation is a real experiment** | **one differing key, 3 reps, same store, prompt-equivalent** |

r12–r15 are new in this wave. Everything below is what they found.

## The ablation result is a NULL, and the harm is real

`logs/evals/p3a-memory-ablation-real.json`, 168 observations through the real
loop with the real Docker verifier:

| lane | memory off | memory on | delta | intervals overlap | attributable |
|---|---|---|---|---|---|
| oblivious (shipped double) | 1.0000 | 1.0000 | `0.0000` | yes | no |
| **grounded (declared memory-reading policy)** | **1.0000** | **0.9286** | **`-0.071429`** | **yes** | **no** |

**On this task set, with this model, memory did not help.** The two arms'
confidence intervals overlap, so the delta is not attributable to memory — and
the point estimate is published anyway rather than dropped, because a null
result that only appears in a "we found no difference" sentence is indistinguishable
from not having looked.

Then the number that matters more than the delta:

```
harm: 3 of 42 pairs moved; 3 of 3 moved FOR THE WORSE
```

All three were `bug01_wrap`, all three repetitions, and all three caused by the
same stale `FILE-ALREADY-FIXED` memory: the grounded agent read "this file was
already fixed in an earlier session", skipped the edit, and the task failed
verification. In the oblivious lane the model never read the memory, so it never
acted on it, and it resolved the task.

**Memory cost nothing and helped nothing, and when it was read it made things
worse.** That is the honest headline, and it is why r14 measures the corpus
rather than assuming it.

## The corpus is measurable, and it is not good enough yet

`logs/evals/p3a-memory-quality.json`:

| metric | value | reading |
|---|---|---|
| precision | `0.666667` (4/6 scored) | 2 of 6 injected memories were irrelevant to the task they were injected into |
| staleness | `0.166667` (1 false / 4 hold / 1 unverifiable of 6) | one asserted something the repository bytes contradict — the `bug01_wrap` one |
| retraction detection | `1.0` (5/5) | a renamed file is detected by the snapshot and by the independent checker |
| retraction amendment | `0` | detection is the only mechanism; **nothing reads the flag** |
| checker/label agreement | 4 agreed, 0 checker gaps, 2 off-scope | the independent checker and the hand labels agree on everything byte-checkable |
| harm | borrowed from `evals.memory_ablation_real` | never recomputed — two answers to one question is worse than one borrowed answer |
| inbox contribution | **0 of 6** | a measured zero: this corpus is seeded straight into a store for determinism |

Three honesty properties worth naming because they were bugs first:

* **9 of 14 tasks recalled nothing at all.** Counted, not scored. The harness
  says `(none recorded yet)`, and left in place that line was scored as a memory
  that named nothing and therefore always "held" — inflating every denominator.
* **`unverifiable` is not `false`.** `check_claim` reports a missing repository
  as severity `high`, because not being able to check a memory *is* serious.
  Folding severity into the verdict published **9 stale memories from 9
  repositories the report had just deleted**. Kind is the discriminator.
* **A whole-suite failure does not refute a claim about one file.** The first
  fixed-file check ran the whole suite, so a bug in `numlib/mathutil.py` refuted
  "numlib/__init__.py was reviewed and needed no change", which is true. An
  unattributable failure is now `unverifiable`, never `false`.

## Trust Ladder rung #7 — now measured, and red

Rung #7 ("Understands the project on day 2") was `not_implemented` with the
reason *"no gate measures cross-session recall yet."* P3a-W2 landed the
measurement it was waiting for, so it now **reads** `evals.memory_quality`
instead of inventing a second one — and it **FAILS**:

```
9 of 14 tasks recalled nothing at all, and of what WAS recalled, 1 asserted
something the repository's own bytes contradict (staleness 0.1667).
```

That is a much better state than `not_implemented`: the reason moved from
"nobody measured" to "measured, and the measurement is a negative result".

## The inbox gate, and the bypass it declares

`evals.inbox_gate` is 5/5 green. The three findings that came out of building it:

1. **A rejection was NOT sticky.** `ProposedMemory.digest` includes
   `session_id`, so the same sentence extracted by the next session hashed
   differently and a human's "no" expired at the end of the session. Fixed with a
   separate `content_digest` that excludes the session.
2. **`suppressed_by_rejection` was publishing the wrong number.** It reported the
   size of the known-digest SET, so a one-item proposal reported "2 suppressed".
   A count of what was suppressed that is really a count of what is known cannot
   be read by anyone downstream. Now two keys: `suppressed_by_rejection` (items)
   and `rejected_digests_known` (digests).
3. **The AST scanner could not name the function it was gating.** `ast.walk` is
   breadth-first, so accumulating "parents" as you go gives no in-scope chain and
   every site was attributed to `<module>`. It also found *zero* write paths
   through the inbox, because `MemoryInbox.apply` reaches the store via
   `store.record` rather than through a call to `.apply(` — which reads as
   "nothing is gated" rather than "the gate is here".

**The declared bypass.** `DecisionStore.ingest_state_file`, driven automatically
by `poll()` and `watch()`, promotes Boundary-4 state-file rows with no human in
the loop. It predates the inbox and is a deliberate contract. It is ENUMERATED by
r13 and named in the report's `claim_scope`, so the trust claim covers the
**extraction pipeline** and not every automated write. Deleting the declaration to
make the numbers look better is a pinned test failure.

## Security: one real receipt gap, closed without changing a contract

The credential-in-memory canary passes through three layers: the shared gate,
the real store, and the inbox's on-disk patch file. Finding it surfaced a defect
in the **receipt**, not the redaction: a memory row containing a live credential
was stored correctly redacted and then reported `reason=""`, `findings=()`,
`severity="none"` — indistinguishable downstream from a row that never held one.

The first fix appended a finding, which **broke a pinned contract**: in `block`
mode any finding refuses the write, so credential-bearing memories started being
quarantined instead of stored-redacted. `test_ceiling_security.py` caught it. The
notice now travels in `reason` as `MEMORY_CREDENTIAL_REDACTED_REASON` — it is a
note about what was removed, not a policy finding — and
`test_p3a_security_repins.py` pins both halves.

## What this does NOT establish

* **No live provider ran.** Every model call in every number above is a scripted
  double. Nothing here is a claim about model quality.
* **The token/cost axis is vacuous.** Every call in both lanes reported exactly
  20 tokens, so the estimator cannot see prompt length. The report publishes
  `cost_axis_vacuous: true` rather than quoting a `$0.00` delta as evidence that
  memory is free.
* **The AHE +5.6pp figure is the corpus's measurement of a different system.**
  Nothing here reproduces it.
* **`recall@k` measures retrieval, not task success.**
* **The token/cost axis of a real run is unmeasured.** Point 1 and point 4 above
  are the same limitation seen from two sides.
* **A stale memory is DETECTED, not AMENDED.** `retraction.amended_count` is 0
  and that is the honest answer: nothing reads the staleness flag yet, so a
  flagged memory still reaches the model.

## Not yet implemented

- `r11` live-provider lane — blocked, no provider reachable.
- **Nothing reads a staleness flag.** Detection without consumption is half a
  control; the consumer is the next thing to build.
- **Ranking has no product consumer.** `memory_ranking_enabled` is read by the
  ablation and by nothing else, so `no_ranking` measures a flag with one consumer.
- **Consolidation artifacts are never read back.** `MEMORY.md` / `USER.md` are
  written and diffed; no injection path consumes them.
- **The extraction queue has no daemon.** A caller must drive `lease()`.
- **`mcp_server` is un-extended.** No `memory_search` / `memory_revoke` tool.
