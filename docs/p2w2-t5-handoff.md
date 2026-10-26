# P2/W2 · Wave 2 · T5 — contracts, memory, evals, CI

**Gate published: `G2` is RED.** `python -m evals.gates.P2` exits 2 —
**7 pass, 3 fail, 2 blocked, 0 not_implemented**, four-status vocabulary only,
`skip` not constructible.

---

## 1. Files changed, with real line numbers

### Created (4 files, 2,883 lines)

| file | lines | what |
|---|---:|---|
| `evals/gates/P2.py` | 1,312 | the G2 gate. Imports its vocabulary from `evals.gates.P0`. 12 rungs, each naming its proving tests; `_selftest()` proves it can fail. |
| `tests/test_p2w2_vendored_proof.py` | 533 | vendored-code audit: 3 file digests + NOTICE digest pinned, 4 header fields, NOTICE currency, licence compatibility, binaries. |
| `tests/test_p2w2_corpus_proof.py` | 540 | corpus audit: defence-removal non-vacuity, EXACT count pins, coverage map. |
| `tests/test_p2w2_policy_proof.py` | 498 | policy language: 3 broken rules refused, example suite, layering. |

### Edited

| file | change |
|---|---|
| `tests/known_failing_pins.py:306-330` | `KNOWN_FAILING_PINS` → `()`; added `NO_RECORDED_GAPS`, `KNOWN_FAILING_PINS_DECLARED_EMPTY`, `…_REASON`; **2 Phase-3 inverted pins** added at the end of `INVERTED_PINS` (now 6). |
| `tests/test_known_failing_pins.py:298-345` | the empty-registry contract: accepts `()` **only** when declared, requires a ≥30-char reason, and requires `INVERTED_PINS` non-empty. |
| `tests/test_ceiling_r2_04_daily_default.py` | **was blocking collection of the whole suite.** 3 legacy-engine tests → 7 inverted pins in `TestTheLegacyEngineStaysGone`; the "restore the old engine" test → a refusal pin; the artifacts differential → a positive claim + a control arm. 23 passed. |
| `evals/gates/__init__.py` | `__all__ = ["P0", "P1", "P2"]`; documents why `P1`/`P2` import rather than re-declare. |
| `INTERFACES.md` | 6 false statements corrected; new **Boundary 0 "WHAT DID NOT CHANGE"** section; Change Log entry (section 2 below). |
| `docs/release-verdict.md` | the **G2** section appended (835 lines total now). |
| `shared/AGENTS.md` | the P2/W2 record. |

### NOT edited (explicitly)

`harness/**` `execution/**` `runtime/**` `cli/**` — read only.
`shared/security_corpus.py` and `shared/policy_lang.py` — **read, measured,
not edited**; see §6 for why editing the evidence mid-audit would have been
the wrong move.

---

## 2. INTERFACES.md — six false statements corrected, each with its measurement

| # | was | is | evidence |
|---|---|---|---|
| 1 | `AgentKernel.STRATEGY_NAMES` is the prescribed fix | `harness.agent_kernel.kernel.STRATEGY_NAMES` — module-level, not a class attribute; `strategy.py` doesn't define it | `AttributeError` |
| 2 | `schedules.py:184` validates the strategy | **`:397`**; `:184` is `_parse_iso` | source read |
| 3 | `run_task(task)` | `run_task(task, log_root=None)` (`core.py:1904`), **plus**: it does *not* route through `agent_step`, and it **maps** a status (`core.py:1954`) rather than minting one | AST |
| 4 | verdict is `PROVENANCE_INCOMPLETE` | `PROVENANCE_GAPS` — that string exists nowhere in the tree | `provenance_report.py:1075` |
| 5 | `--strict` → exit 0 | exit 0 **now** (T1 reconciled the headers); the entry previously contradicted *itself* 15 lines earlier | re-measured |
| 6 | mint count is **FOUR** | **THREE**, and the divergence pair is now a **single** divergence (`legacy.py` is deleted) | `MINT_SITES_EXPECTED = 3`; AST = 3 mint / 1 mapping / 8 comparison |

Also repaired: a **fused paragraph** at Boundary 1 (`…generously at 2am.- \`execute_sandboxed\` runs…`) in the section whose own stated failure mode is "a sentence read generously at 2am"; and a **duplicated clause** in the provenance entry.

**The new Boundary-0 section** states the five invariants that did *not* move (`RUN_STATUSES` still exactly 7, `Task` still exactly 4 fields, MCP still 5 tools, no signature removed/renamed/made-required) — **and names the one that is broken in the same place**, so the reassurance paragraph is not a place where an exception is hidden.

---

## 3. Raw output — all ten commands

### 3.1 `python -m pytest tests/ -q -p no:randomly`
```
101 failed, 7493 passed, 33 skipped, 2 warnings in 5003.01s (1:23:23)
```
0 collection errors. Worst files: `test_agent_loop.py` 28,
`test_agt_02_reflection.py` 16, `test_agt_10_batch_boundary.py` 10,
`test_cli_tui.py` 6, `test_aesthetic_gate.py` 6, `test_command_types.py` 5,
`test_cli_tui_layout.py` 5, `test_trust_ladder.py` 3.

### 3.2 `pytest tests/test_ceiling_security.py tests/test_security_regressions.py tests/test_mcp_adversarial.py -q`
```
106 passed, 5 skipped in 11.98s
```
The 5 skips are pre-existing Windows symlink-privilege cases — **not passes**.

### 3.3 `python -m pytest tests/test_known_failing_pins.py -q`
```
18 passed in 16.31s
```
`python -m tests.known_failing_pins` → `counts: as_recorded=6`, `0 build failure(s)`, **exit 0**.

### 3.4 `python -m evals.run --check`
```
prompt task set: 14/14 ok
verdict: CLEAN
EXIT=0
```

### 3.5 `python -m evals.run --suite trust-ladder`
```
counts: pass=2, fail=2, blocked=5, not_implemented=1  (verdict MEASURED_RED)
measured here: 8, 9, 10
carried:       1, 2, 3, 4, 5, 6, 7
EXIT=2
```
Rung #10's failures are T4's `cli/runview.py` projection defects (LINK 3
attribution, `retrieval_truncated` dropped, no absent-value vocabulary, an
unpriced `$0.000000`). Every model call in the ladder is a scripted double.

### 3.6 `python scripts/provenance_report.py`
```
provenance: PROVENANCE_COMPLETE
  scanned 702 source files
  declared:    3
  undeclared:  0
EXIT=0     (--strict: STRICT_EXIT=0)
```

### 3.7 `python -m ruff check <every file this round created or edited>`
```
All checks passed!
```
Whole-scope `ruff check shared evals memory tests` reports **58 findings**,
none in a file this round created (12 were in my new files and were fixed with
a **scoped** `--fix` — unscoped `--fix` has already stripped two deliberate
`# noqa`s in this repo's history).

### 3.8 `python -m compileall -q shared evals memory`
```
EXIT=0
```

### 3.9 `git diff --check`
```
exit 0
```
(only this tree's pre-existing LF→CRLF warnings)

### 3.10 `docker info`
```
Containers: 1     Images: 80
Server Version: 28.5.1
Operating System: Docker Desktop
CPUs: 12          Total Memory: 7.614GiB
EXIT=0
```
**Docker is the one Phase-2 precondition that is green**, and it is reported
as green precisely because three others are not.

### 3.11 My three suites
```
tests/test_p2w2_corpus_proof.py   22 passed
tests/test_p2w2_policy_proof.py   25 passed
tests/test_ceiling_r2_04_daily_default.py  23 passed
                          → 71 passed in 10.38s
tests/test_p2w2_vendored_proof.py 18 passed, 8 FAILED  (see §6)
```

---

## 4. Corpus non-vacuity transcripts

`python Temp/opencode/w2_1_nonvacuity.py`. Each defence is replaced by an
identity **in-process**, the corpus is re-probed, then the original is restored
and the restored state is asserted equal to the original.

```
DEFENCE 1 -- shared.security.review_untrusted_source  (the 7-source boundary)
cases probed          : 15
before (defence ON)   : 15 true
with defence OFF      : 0 true
cases that MOVED      : 15
    issue-canary: True -> False
    agents-md-canary: True -> False
    fetched-page-canary: True -> False
    skill-body-canary: True -> False
    plugin-manifest-canary: True -> False
    mcp-result-canary: True -> False
    memory-row-canary: True -> False
    issue-canary-bidi-override-strict-body: True -> False
    issue-canary-ansi-split-strict-body: True -> False
    agents-md-canary-zero-width-split-strict-body: True -> False
    skill-body-canary-zero-width-joiner-strict-body: True -> False
    mcp-result-canary-ansi-split-strict-body: True -> False
    ... and 3 more
restored == before    : True
VERDICT (untrusted-source boundary) : LOAD-BEARING  [15 case(s) caught by a case going red]

DEFENCE 2 -- shared.security.redact_text  (a secret surviving into output)
cases probed          : 8
before (defence ON)   : 0 true
with defence OFF      : 2 true
cases that MOVED      : 2
    issue-canary-bidi-override-strict-body: False -> True
    memory-row-canary-bidi-isolate-strict-body: False -> True
restored == before    : True
VERDICT (canary exfiltration) : LOAD-BEARING  [2 case(s) caught by a leak appearing]

DEFENCE 3 -- shared.security.normalize_for_redaction  (invisible folding)
cases probed          : 8
before (defence ON)   : 0 true
with defence OFF      : 1 true
cases that MOVED      : 1
    issue-canary-bidi-override-strict-body: False -> True
restored == before    : True
VERDICT (obfuscation normalisation) : LOAD-BEARING  [1 case(s) caught by a leak appearing]

DEFENCE 4 -- shared.security.defang_untrusted_delimiters  (boundary breakout)
cases probed          : 7
before (defence ON)   : 7 true
with defence OFF      : 2 true
cases that MOVED      : 5
    breakout-end-boundary: True -> False
    breakout-begin-boundary: True -> False
    breakout-openhands-tag: True -> False
    breakout-taint-marker-forge: True -> False
    breakout-quarantine-forge: True -> False
restored == before    : True
VERDICT (delimiter breakout) : LOAD-BEARING  [5 case(s) caught by a case going red]
```

**Counts, pinned EXACTLY** (closing the `>=` assertions that let three declared
threat-model categories sit at zero): 23 / 7 / 8 / 9 / 7 / 13 = **67**.
Coverage: `hardline` 6, `fuzzy_edit` 2, `approval_gate` 2, `approval_origin` 1,
`containment_receipt` 1, `vendored` 1. Every coverage reference is resolved or
the test fails.

**Named uncovered:** `containment_receipt_forgery` (no production path mints a
receipt a task could forge) and `vendored_file_tamper` (the gate enforced the
directory, not the file — until this round's digests).

---

## 5. The three broken-rule refusals

`python Temp/opencode/w2_2_transcripts.py`.

**1 — a `match` example that does not match**
```
REFUSED at load, as required.
  message : rule 'w2_broken_match' (layer 'defaults') example 'command=git' does not behave as declared: expected 'deny' and for this rule to be the deciding rule, got 'ask' decided by 'layer_default'
  rule    : 'w2_broken_match'   layer : 'defaults'   example : 'command=git'
```
**2 — a `not_match` example that DOES match**
```
  message : rule 'w2_broken_not_match' (layer 'defaults') example 'tool=shell' does not behave as declared: the rule MATCHES this subject, so it cannot be listed as one it does not apply to
```
**3 — a malformed decision**
```
  message : unsupported policy decision 'forbiddenish'; expected one of ['allow', 'ask', 'block', 'deny', 'forbidden', 'permit', 'prompt'] (rule 'w2_bad_decision')
```
Bonus: a `floor` rule carrying `allow` → *"the floor layer only carries denials"*.

**Two of my first three were refused for the WRONG reason** — by the example
*grammar* for a bare token (`command=git status`), which points the reader at
the wrong defect. That is the failure this round exists to catch and my own
transcript reproduced it; it is recorded in place.

**Layering, same three rules, four orders:**
```
order=defaults > builtin_agent > user_config > per_agent > per_session -> 'deny' by 'l_session'
order=defaults > user_config > per_session                               -> 'deny' by 'l_session'
order=per_session > user_config > defaults                              -> 'ask' by 'l_defaults'
order=defaults > per_session > user_config                              -> 'allow' by 'l_user'
```
Three distinct answers from three identical rules. The floor beats all of them
in every order, `floor=True`.

**Example suite: 29 rules, 75 examples, 0 failing, 0 rules shipping without
one.** (It moved during the round: 9/34 → did-not-load → 29/75. That movement
is why it is measured continuously rather than pinned once.)

---

## 6. Vendored-code inventory, and what the gate does NOT check

| file | upstream | licence | byte copy | sha256 |
|---|---|---|---|---|
| `harness/third_party/opencode_replacer.py` | sst/opencode | **MIT** | **No** | `ce795f0f50c4…` (was `d3087952ba47…`) |
| `harness/third_party/aider_bitap.py` | Aider-AI/aider | **Apache-2.0** | **No** | `f06abcb390aa…` (was `452658babd6b…`) |
| `harness/third_party/__init__.py` | — | — | package marker | `c81860a5df61…` |

Four header fields present on both modules; reason fields clear a 200-char
floor (the gate's own floor is 15). Each docstring states **what was NOT
taken**. Licence compatibility recorded: both permissive; Apache-2.0 §4 is why
`NOTICE.md` exists and why `aider_bitap.py` must never be re-declared MIT.

**Binaries:** ripgrep **14.1.1** (`MIT OR Unlicense`), fd **10.2.0**
(`MIT OR Apache-2.0`) — both corrected against the brief that said "MIT".
Versions pinned in `harness/search_engine.py`; **neither SHA-256 is committed**
and `provenance/` is **gitignored**, so the manifest is a claim no reviewer can
see in a diff. Rung #8 `fail`.

### The finding: `provenance_report.py` never compiles the file

`scripts/provenance_report.py` reads the header as **text**. It does not parse
the file. So a file can carry a complete, valid, seven-field declaration and be
a Python `SyntaxError`, and the gate reports the tree COMPLETE.

That is not hypothetical — it happened **during this audit**. T1 reconciled the
header vocabulary (rung #5 went `fail` → `pass`) by inserting the `# vendored:`
block **above the module docstring**, which removes the docstring's opening
triple-quote:

```
harness/third_party/aider_bitap.py        SyntaxError line 10
harness/third_party/opencode_replacer.py  SyntaxError line 17
```

while `provenance_report.py` exited **0**, `declared: 3, undeclared: 0`. **The
gate went green on two files that could not be imported.** My 8 failing
`test_p2w2_vendored_proof.py` tests are the pins doing their job: checksum
drift ×3, the parse check, the docstring check, the four-field check ×2, the
reason-specificity check, and the licence check. Owner **T1**.

I did **not** repair them: `harness/third_party/` is not mine, T1 is actively
editing, and writing over a shared worktree's in-flight file is the collision
this repo's protocol forbids.

---

## 7. The G2 verdict, all seven elements

In `docs/release-verdict.md` from line 448. The gate is
`python -m evals.gates.P2 [--json|--selftest|--full-suite-log PATH]`.

| rung | status | one line |
|---|---|---|
| #1 unsandboxed-bash closed | `pass` | 75 pins pass; registry entry deleted in the same change |
| #2 untrusted sources scanned | `pass` | 7 sources; removing the boundary moves 15/15 |
| #3 policy fails closed | `pass` | 3 broken rules refused; 29 rules / 75 examples; order-dependent |
| #4 corpus non-vacuous | `pass` | 67 cases, EXACT pins, 4 defences removed and re-probed |
| #5 vendored provenance | `pass` | green after T1's fix — but see §6 |
| #6 verifier mint unchanged | `fail` | pin passes; it records a gap — 3 sites vs the doctrine's 1 |
| #7 hardline floor wired | `blocked` | no production call site |
| #8 binary digests pinned | `fail` | versions pinned, digests not |
| #9 live provider | `blocked` | never run; scripted doubles throughout |
| #10 full suite | `fail` | 101 failed / 7,493 passed / 33 skipped |
| #11 registry gate | `pass` | 18 passed |
| #12 Docker | `pass` | 28.5.1 reachable |

All seven required elements are present: rungs with named proving tests; the
**30-row attack table with every row listed and zero successes** plus the
caveat that two of the guards are unwired; the **LOC delta in both directions**
(`71 files, +20,611/−2,604`, `harness/agent_kernel/legacy.py` deleted, and the
honest note that the numbers cover tracked files only so insertions are an
understatement); the **8 containment limits reproduced**; what is still blocked
with exact errors; the **vendored inventory with licences**; and the
**unsandboxed-bash closure with the pin that proves it**.

---

## 8. The Phase 3 handoff brief

### What P3a inherits

* **Two inverted pins are PRE-REGISTERED** in `tests/known_failing_pins.py`,
  both owner **P3a**, so Wave 1 needs no registry negotiation:
  * `test_the_mint_count_is_not_one_because_the_doctrine_says_so` — green
    because the count is 3 while `DOCTRINE.md` §2 says 1.
  * `test_the_three_mint_conditions_are_recorded_with_their_exact_differences`
    — green because `verified.py` reads an error-bearing evidence block as
    clean (no `not error` term).
  * Both say in their own `goes_red_when` that the closure is a **behaviour**
    change requiring a Change Log entry, a retarget of the pin, and — for the
    first — an update to `DOCTRINE.md` §2 **in the same change**.
* **The mint sites** (`MINT_SITES_EXPECTED = 3`): `completion.py:175`,
  `verified.py:150`, `agent_loop_step.py:987`. `harness/core.py:1954` is a
  **mapping**, not a mint.
* **`KNOWN_FAILING_PINS` is `()` and declared so.** Do not delete the
  declaration to tidy up; the meta-test now *requires* it.

### What P3b inherits

* Rung #7 (`blocked`): the hardline floor has no production call site. The
  call sites T3 already names are `execution/workspace.py::SafeToolBackend.execute`
  and `execution/sandbox.py::execute_sandboxed`. Until it is wired, **every
  hardline bypass row in the attack table is a property of the TABLE, not of a
  running guard.**
* The aux-LLM gate (`harness/approver.py`, `runtime/approver_gate.py`) also has
  **zero** production callers.

### The trust baseline

**7 of 12 G2 rungs pass. Four are red or blocked. The full suite is red at
101.** The trust claim that IS established: the untrusted-source boundary is
load-bearing on all 15 canary cases; the corpus cannot shrink; the policy
language refuses to load a rule that cannot prove itself and its layering is
order-dependent; the unsandboxed-bash path is closed and pinned closed.

The trust claim that is **NOT** established: anything about model behaviour
(every model call is a scripted double), anything about a running floor or gate
(neither is wired), and anything about the tree being green.

---

## 9. What no prompt anticipated

1. **The shipped policy refused to load TWICE**, both times on
   `floor:hardline:language_runtime_destructive_call`'s own declared example,
   because T3's `screen_or_raise` returned `None` (allowed) for
   `ruby -e FileUtils.rm_rf "/"` while refusing the neighbouring shapes. **The
   mechanism worked and a real defect existed simultaneously** — a policy
   language validating its own examples caught a rule whose declared proof was
   false, with no human reading either file. It is the strongest argument for
   the design and it is why the gate had to be made total.

2. **My first `evals/gates/P2.py` CRASHED on its first run** with exactly that
   error. **A gate that dies on a broken input reports nothing, and "no report"
   reads like "no problem."** `policy_measurement()` is now total and returns a
   refusal as a *value*; every rung is wrapped by `_safe()`.

3. **The provenance gate never compiles the file it declares.** It went green on
   two syntactically invalid vendored modules (§6).

4. **The corpus overstates its own mechanism.** Its header says every obfuscated
   canary is framed so a strict body class is the *only* defence. Measured:
   **1 of 8**. `shared/AGENTS.md`'s "5 of 7 load-bearing" is now 1 of 8. Pinned
   as a measurement with a band (`>=1`, `<=3`).

5. **`provenance/` is gitignored**, so `binaries.json` — described in
   `provenance_report.py:466` as "a committed JSON document" — is a claim no
   reviewer can see in a diff.

6. **I committed the exact bug I was documenting.** Writing the parse-check
   docstring, I put an escaped triple-quote inside a triple-quoted docstring,
   which terminated it and left `tests/test_p2w2_vendored_proof.py` unparseable.
   The symptom was *identical* to the bug in §6, which is why the check is
   `ast.parse` and never a visual inspection.

7. **Three of my own `INTERFACES.md` "fix this" instructions were unfollowable**
   — `AgentKernel.STRATEGY_NAMES` (`AttributeError`), `schedules.py:184` (the
   wrong function), and a duplicated clause. Two earlier drafts of my own
   substring checks were also wrong, on the documented "a gate matching a bare
   substring cannot tell a definition from a reference" trap: one scanned for
   `rm -rf` (a rule's own `match` example may contain one) and one for
   `HARDLINE_PATTERNS` (the fold *references* T3's attribute). Both were
   rewritten in AST.

---

## 10. Deliberately NOT done

* **`shared/security_corpus.py` is unedited.** The measurement argues 7 of the
  8 obfuscated rows should be re-cut so they genuinely need normalisation — but
  editing an evidence file mid-audit invalidates the non-vacuity transcripts
  that justify the change. Deferred with the measurement attached.
* **The 53 remaining `legacy_agent` pins in `tests/` are not swept.** I fixed
  the one file that was blocking collection of the whole suite; the other 53
  are mechanical and are this terminal's own debt, named as rung #10's owner.
* **The provenance gate's parse blind spot is pinned, not fixed.**
  `scripts/provenance_report.py` is not my file.
* **`provenance/` is still gitignored.** `.gitignore` is not mine.
* **The 101 full-suite failures are not adjudicated one by one.** 54 are named
  precisely; the other 47 are not guessed at.
* **No full-suite claim beyond the one run.** 7,493 passed is a count, not a
  verdict.