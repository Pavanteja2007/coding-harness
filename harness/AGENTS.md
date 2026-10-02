## P0-W2-T1 — Wave 2: pin everything Wave 1 fixed, so it cannot return

**Round:** P0 Foundation, Wave 2, T1 (harness). **Created four
`harness/`-local test modules and edited NO production file.** `harness/**`
only; `cli/**`, `execution/**`, `runtime/**`, `shared/**`, `memory/**`,
`evals/**`, `tests/**`, `scripts/**` and `.github/**` were **read and not
written**. The one incidental edit is a one-character whitespace repair in this
file (§9 below).

**The wave in one sentence: a fix without a pin is a fix that returns, so every
guarantee Wave 1 established now has a test that fails the day someone reopens
it — and every guarantee that is currently VIOLATED has an inverted pin naming
who owns closing it.**

Four new files, 3148 lines, 50 tests, all passing:

| file | pins | lines |
|---|---|---|
| `harness/test_egress_call_sites.py` | the redaction boundary, structurally (T1.W2.1) | 1156 |
| `harness/test_mint_site_pins.py` | the mint-site count and both condition divergences (T1.W2.2) | 732 |
| `harness/test_gap_audit.py` | every TODO/FIXME/HACK/placeholder, classified (T1.W2.3) | 746 |
| `harness/test_structural_claims.py` | the three structural claims (T1.W2.4) | 514 |

### 1. T1.W2.1 — the redaction boundary is pinned by AST, not by behaviour

**Wave 1's redaction tests are behavioural**: they drive a value through a path
and assert the secret is gone. That is the right test for a path somebody
exercised, and it is **blind to the failure mode this wave exists to prevent** —
a NEW call site added tomorrow in a module nobody ran a secret through. A
behavioural suite passes for every path somebody happened to exercise.

So `harness/test_egress_call_sites.py` parses every `.py` under `harness/`
(excluding `_stubs` and this file) and classifies each one by **provenance**,
which is the question a grep cannot answer: *does the payload this call builds
reach a journal or a result, and has it been through the boundary?*

**Four verdicts, deliberately not collapsible** — a binary "is it redacted"
would make an allowlist entry indistinguishable from a pass, and the whole value
of an allowlist is that it is a *different* answer a reader can audit:

| verdict | meaning | evidence required |
|---|---|---|
| `redacted` | the expression routes through a redaction helper inline | the AST |
| `redacted_by_receiver` | the SINK redacts the whole payload | a live proof, per receiver |
| `allowlisted` | declared exempt | a written reason + an owner |
| `unredacted` | the failure | — |

**The measured result: 19 sites, 0 unredacted, 7 allowlisted, 13 redacted by the
sink, 0 inline.** `enumeration_table()` publishes the per-file breakdown.

Two design decisions that took measurement rather than argument:

* **A bare `append` is not a sink.** The tree has ~200 `x.append(...)` calls;
  treating each as an egress produced 200 candidates of which the overwhelming
  majority are `lines`, `out`, `parts` and `messages`. The discriminator is
  `JOURNAL_RECEIVERS` (measured by hand: `trace`, `run_trace`, `events`,
  `journal`, `machine`, `_st`, `commands`) **plus** a string-literal event-kind
  first argument. Both halves are needed and both halves are proven against live
  code in `test_the_scanner_separates_a_journal_emit_from_an_in_memory_append`,
  which asserts `core.py` still has ≥10 `messages.append(...)` carrying a
  carrier and that none of them is reported.
* **A `redacted_by_receiver` verdict must be proven, not assumed.** `test_a_
  redacting_journal_receiver_really_redacts` drives a live secret through
  `TraceLogger.log` and `RunEventJournal.append` and checks the **on-disk bytes**;
  `test_a_journal_receiver_is_declared_redacting_only_with_proof` asserts the
  set of such receivers is a subset of the proven set, so adding `"lines"` to
  `REDACTING_JOURNAL_RECEIVERS` cannot silence the tree on the strength of a name.

**The scan is NOT sufficient, and says so.** It reads provenance by name, so it
cannot follow a value across a string concatenation. The one real gap this audit
found is exactly that case, and it is recorded in `RECORDED_GAPS` with a
behavioural proof rather than pretending a classifier reached it — see §2.

### 2. The one real un-redacted egress site in `harness/`, recorded not fixed

`harness/tools.py::_precommit_refusal` builds its message like this:

```python
message = f"the edit to {...} was refused before it was written: {...}"
if receipt.get("context"):
    message += "\n" + str(receipt["context"])
return ToolResult(..., error=message)
```

`receipt["context"]` is **real source bytes** — `lint._context_block(src, line,
span)`, the ±3 lines around a syntax error — and it reaches a `ToolResult`
**without being redacted**, while `lint.render_check` redacts the very same field
of the very same receipt. One receipt, two renderers, opposite answers.

* **Measured, not asserted.** `test_the_known_gap_is_still_open_so_this_file_is_
  not_claiming_a_fix` drives `lint.check_source_for_edit` with a candidate whose
  context block holds `sk-A…A`, then `_precommit_refusal`, and asserts the secret
  **survives** in the rendered `ToolResult.error`. It also asserts the contrast:
  `lint.render_check(check)` does NOT leak. That pair is what makes the gap a
  defect rather than a policy.
* **Reachable through `TypedToolRuntime.execute`**, whose only callers today are
  tests — so this is a structural hole, not a live leak.
* **Owner: T1, one line** — route `receipt['context']` through
  `redact_text_for_journal` as `lint.render_check` already does.
* **NOT fixed in this wave.** Wave 2 is explicitly forbidden from implementing a
  recorded gap. `RECORDED_GAPS` holds it with an owner and a named behavioural
  proof; `test_a_recorded_gap_that_gets_closed_is_reported` inverts on it, so the
  day someone fixes it the record has to be deleted in the same change.

### 3. T1.W2.2 — the mint count is pinned at FOUR, and the doctrine's "one" is pinned too

`harness/test_mint_site_pins.py` re-derives the mint/mapping/comparison
classification by AST and asserts `MINT_SITES_EXPECTED = 4` plus the four named
files. **The prompt asked for "exactly the known-single site"; the tree has four
and `phases/DOCTRINE.md` §2 claims one.** Asserting 1 would require editing a mint
*condition*, which this wave forbids, so the pin asserts what is true and a
second pin asserts the **gap between the doctrine's claim and the tree**:

`test_the_mint_count_is_not_one_because_the_doctrine_says_so` reads
`phases/DOCTRINE.md` and, while it still says "exactly one place", fails if
`MINT_SITES_EXPECTED` becomes 1. So the day the four are collapsed — the real fix
this round recorded — the doctrine has to be updated in the same change, and the
divergence cannot go stale silently.

**Both condition divergences are pinned as EXACT facts, including their absence:**

| divergence | how it is pinned |
|---|---|
| `legacy.py` defaults a MISSING `regression_passed` to `True` | `_get_call_arguments(legacy_run, "regression_passed") == "True"` — parsed, not substring-matched, because the whole divergence IS the default |
| `verified.py` + `legacy.py` read an error-bearing evidence block as clean | `assert "error" not in _assignment_rhs(run, "clean")` — read from the `clean = …` assignment, not the whole function, because `run()` legitimately mentions `error` when handling verifier failures |

Both assert the divergence is **still present**. A tightening would fail these
and ask for the record to be updated, which is the direction the brief asks for
(inverted pins) and also the right engineering discipline: a verifier that
crashed and left stale booleans must not mint a pass, but that fix is a
behaviour change and has to be a *recorded* one.

`test_the_three_mint_conditions_agree_across_the_tree_still_passes` asserts T5's
behavioural half is **still there by name** — a divergence pin in a file nobody
runs is not a pin.

**Three measurement notes worth keeping:**

1. **The condition is not always in the same function as the mint.** The pure
   core mints `… if clean else …` and `clean` comes from `_evidence_is_clean`,
   ~230 lines below. A "read 12 lines above the mint" check — the shape Wave 1
   used — passes **vacuously** for that site. Every condition is now resolved by
   function NAME (`_function_body`) and the mint sites by AST.
2. `harness/AGENTS.md` records the fourth mint as
   `agent_loop_step.py:896-901`. Line numbers are not used anywhere in the new
   pin for exactly this reason.
3. The scan reads `utf-8-sig` and **does not skip a file that fails to parse**.
   The first draft had `except SyntaxError: continue`, which is a silent hole
   with a specific shape: a UTF-8 BOM makes `ast.parse` raise on line 1, and a
   skip turns "I could not read this file" into "this file is clean". This bit
   me for real — `tests/test_config_trace_state.py::test_harness_modules_import_
   and_parse` went red on a BOM I introduced with a PowerShell `Set-Content`.

### 4. T1.W2.3 — the marker-word audit is complete, and the gap it found is not a marker word

`harness/test_gap_audit.py` reads every `TODO` / `FIXME` / `HACK` / `XXX` /
`not implemented` / `for now` / `temporarily` / `placeholder` / `not yet` in a
**comment or docstring** across `harness/` (including `_stubs/`, because that is
where a hand-rolled replacement for a real boundary goes).

**Ten hits. All ten read, all ten classified as DECISIONS with a written reason.
Zero gaps.** The reason the classification is per-hit rather than per-word:

* **the `todo` TOOL.** `harness/tools.py`'s catalog has a `"todo"` entry, the
  kernel registers a `todo` handler, `SessionState.todo_items` is a field. A
  case-insensitive grep for `todo` returns all three plus every real marker. This
  is the single strongest argument for reading hits rather than counting them.
* **two NEGATIVE declarations.** `harness/trace.py:8` is *"Redaction is NOT
  implemented here"* and `harness/redaction.py:40` is *"No secret pattern, no
  allow-list, no placeholder of our own"*. Grepping finds the sentences that say
  the thing is implemented **somewhere else**, which is the opposite of a gap.
* **`"not yet"` means two different things.** `steering.py:997` ("PENDING seq
  **not yet** recorded as queued") is an ordinary adverb about queue state;
  `conversation.py:1065` ("the handoff is **not yet** counted against the
  protected region") states a real accounting gap in a comment with no pin. Same
  regex, opposite classifications — and the second is the closest call in the
  table, so its reasoning is written out in full.

**And then the audit ran `pytest harness/`, which is what an audit of this
directory should do — and found a defect no marker-word scan would ever surface:**

> `harness/test_config.py` is a **production** module (R2-03's
> test-configuration guard) named `test_*.py`. pytest collects it, then collects
> its public `test_config_guard(pristine_dir, work_dir)` **function** as a test
> with two fixtures that do not exist. Every `pytest harness/` run ends in
> `ERROR harness/test_config.py::test_config_guard: fixture 'pristine_dir' not
> found`.

Recorded in `GAPS` with owner **T1 + T5** (rename the module to `config_guard.py`,
or the function to `evaluate_test_config_guard`, and update the seven call sites
in `tests/test_ceiling_r2_03_config_guard.py`). Its inverted pin
(`test_the_test_config_module_collection_error_is_still_there`) **runs pytest and
asserts the error is still there**, so the gap is established on every run of
this file rather than on the day somebody noticed it.

**This gap is the answer to the round's real question.** The brief asked for
every marker word to be classified, and all ten are — but `phases/DOCTRINE.md`
§3's concern is *"an unrecorded gap just gets rediscovered"*, and the gap that
actually exists in `harness/` this round is not a marker word at all.

### 5. T1.W2.4 — the three structural claims, each pinned by name

`harness/test_structural_claims.py`, 13 tests:

* **Journal-redactor linearity.** `redact_text_for_journal("y"*40000)` under 2.0 s
  (measured 0.025 s); a **4× ratio assertion** under 12× (measured ~4×) because
  a quadratic return PASSES any single-size budget and fails only on the shape —
  `"y"*40000` quadratic was a few seconds, not minutes, so the absolute budgets
  alone do not catch it; plus a 1 MB absolute bound (measured 0.886 s) because a
  ratio can be perfect and still hopeless. One test drives the real
  `TraceLogger.log` write so the claim is about a live journal write and not
  about a helper called in isolation.
* **The four `step()` purity pins: all four verified present, all four passing.**
  They live in `tests/test_agent_loop_matrix.py` (T5's lane; this round did not
  edit that file and cannot add to it). Read by name, so a rename or a deletion
  fails here. **None was missing.** Two things go beyond presence:
  `test_the_purity_pins_are_wired_into_a_passing_suite_not_just_present` runs
  `pytest --collect-only` and asserts each name appears in the collection — a
  pin that is defined and never collected is a comment; and the module-global pin
  is **deliberately duplicated** here as a nine-line structural check, which is
  the one duplication in this round that is right (it is the property most likely
  to be broken by an innocuous edit, and the matrix still owns the behavioural
  consequence). The failure message says to **restore in `tests/`**, not to add a
  copy here.
* **`approve()` defaults to `False`** — asserted by **calling** it on the base
  class AND on a subclass that overrides only `ask`/`invoke`, because that second
  shape is what every real boundary takes. Plus the source-level check that the
  literal `False` is still in `LoopEnvironment.approve` (so the answer cannot
  arrive from a metaclass), plus the other four fail-safe defaults, plus
  `ask`/`invoke` still raising `NotImplementedError` — because a boundary that
  answers its own required capabilities cannot distinguish "nobody wired this"
  from "the tool ran".

### 6. Every demonstration, and what each proved

Both transcripts are in the Handoff. What they established, beyond the
transcripts:

| demonstration | what it caught that reading the pin did not |
|---|---|
| `harness/_w2_bad_site.py` — an un-redacted `ToolResult(True, context["stdout"], …)` | the classifier finds a **tier-1** carrier through a **string-key subscript**. The first draft's `_identifiers` read only `Name`/`Attribute` nodes, so `context["stdout"]` was invisible. That is the same concatenation blind spot that hides the real gap — the demonstration found it. |
| `harness/_w2_bad_mint.py` — `status = CompletionStatus.COMPLETED_VERIFIED.value` | four separate pins fired, not one: the count, the file set, the "downstream of evidence" check, and the declared-set check. The brief asked for a count assertion; the file-set assertion is the one that would have caught this specific bad site with a better message. |
| `harness/_w2_bad_todo.py` — a bare `# TODO:` | the marker scan reads **comments via `tokenize`** (a comment is not an AST node) and does not read string literals, so the tool named `"todo"` is correctly not a hit. |

### 7. Verification actually run (this tree, `-p no:randomly`)

- `python -m pytest tests/test_agent_kernel.py tests/test_agent_loop_matrix.py
  tests/test_tool_protocol.py -q` → **171 passed, 2 skipped**, exit 0.
  (The 2 skips are the `cancel` row's real-adapter runs — AGT-11 records them as
  covered elsewhere. BLOCKED, not passes.)
- `python -m pytest tests/test_verification_gate_wiring.py
  tests/test_config_trace_state.py tests/test_stubs_and_deps.py -q` →
  **51 passed**, exit 0, **after** the BOM repair below.
- `python -m pytest tests/test_ceiling05_knowledge.py
  tests/test_agt_05_plan_isolation.py -q` → **75 passed**, exit 0.
- `python -m evals.run --check` → **14/14 CLEAN**, exit 0. No prompt changed, so
  this is a no-regression receipt and not a claim about model quality.
- `python -m ruff check harness` → **6 findings, none in a file this round
  created**, all pre-existing: `harness/_stubs/model_router.py` (1),
  `harness/agent_kernel/kernel.py` (1), `harness/docs_lookup.py` (4). Proven
  rather than assumed for the two tracked files: piped from `git show HEAD:<path>`
  into ruff and reported the same findings (1 and 4); `kernel.py` is untracked
  (`??`) so HEAD has no version to compare. **All four new files: `All checks
  passed!`**
- `python -m compileall -q harness` → exit 0.
- `git diff --check -- harness/` → exit 0 after one repair (§9).
- `python -m pytest harness/ -q -p no:randomly` → **101 passed, 1 error**. The
  error is `harness/test_config.py::test_config_guard` — **the recorded gap**,
  §4. It is reported here rather than hidden, and
  `test_the_test_config_module_collection_error_is_still_there` asserts it is
  still present.
- **No Docker lane and no live-provider lane were run, and neither is claimed.**
  Every required lane above is host-only. No credential was inspected.

**One red result happened during this round, was mine, and was repaired:**
`tests/test_config_trace_state.py::test_harness_modules_import_and_parse` failed
with `SyntaxError: invalid non-printable character U+FEFF` on
`harness/test_gap_audit.py`. Cause: a PowerShell `Set-Content -Encoding utf8`
edit I made to normalise the `DECISIONS` table wrote a **UTF-8 BOM**. The three
BOM bytes were stripped, the file re-reads clean, and the suite is **51 passed**
above. The incident is worth recording rather than quietly fixing because it is
the exact failure mode `test_the_scanner_finds_sites` and
`_harness_sources()` now guard against, and because it proves the guard is
reachable: the BOM made `ast.parse` fail on line 1, and a scanner that SKIPPED
unparseable files would have reported this file as clean.

### 7a. A pre-existing order-dependent test bug, root-caused, NOT mine

The required lane above is green in **7 of 11 runs** and red in 4, and the cause
is **not** this round and **not** a flake:

> `tests/test_agent_loop_matrix.py` (untracked, T5's lane, untouched here)
> — `TestTheMatrix::test_the_pure_step_reaches_the_expected_status[tool_failure]`
> fails with `expected a 'reflection' event, got ['tool_call', 'tool_result',
> 'tool_call', 'tool_result', 'tool_call', 'verify_skipped', 'task_end']`.

**Root cause, measured.** `MATRIX` is a module-level list of `Case` objects, and
the `tool_failure` case's tool table is built by a CALL, not a constant:

```python
tools={"bash": _clash()},          # tests/test_agent_loop_matrix.py:568
```

`_clash()` (`:441`) returns a closure over a `state: Dict[str, bool]`, so **one
closure is shared by every test that parametrizes on this case** — four of them
(`test_the_pure_step_…`, `test_a_refusal_is_never_charged_…`,
`test_the_real_adapter_agrees_…`, `test_the_real_adapter_leaves_a_replayable_…`).
Whichever of the four runs FIRST consumes the "first failure per command"; every
later one gets `ok=True` and therefore produces no `reflection` event.

Proven three ways, so this is a diagnosis and not a guess:

```
# the shared closure, called twice:
first  call ok: False
second call ok: True   <-- same closure, second call SUCCEEDS

# order A (any order) -> both pass
$ pytest ".../test_the_pure_step_...[tool_failure]" ".../test_a_refusal_...[tool_failure]" -q -p no:randomly
2 passed

# order B (refusal-budget first) -> the pure-step test fails
$ pytest ".../test_a_refusal_...[tool_failure]" ".../test_the_pure_step_...[tool_failure]" -q -p no:randomly
1 failed, 1 passed
```

**Why it looks intermittent and why `-p no:randomly` "fixes" it.**
`pytest-randomly` 5.0.0 **is** installed on this host, so it reorders tests with a
per-run seed. Whether this test loses the race is therefore a coin flip **per
run**, and it is not a load-sensitive flake at all:

| lane | `-p no:randomly` | default (randomly on) |
|---|---|---|
| `test_agent_kernel + test_agent_loop_matrix` | **142 passed, 2 skipped — 4/4 runs** | 1 failed — 4/5 runs |
| `+ test_tool_protocol` (the required command) | **171 passed, 2 skipped** | 1 failed — 4/6 runs |
| the single case, standalone | 14 passed — 4/4 runs | — |

**Not fixed here: `tests/` is T5's lane and this round may not edit it.** The fix
is one line in the `Case` constructor — make the table a FACTORY so each test
gets a fresh closure, e.g. `tools: Mapping = field(default_factory=dict)` plus
`tools=lambda c: {"bash": _clash()}` resolved in `_tools_for` — or give each of
the four tests its own `_clash()`. **Filing to T5 with the reproducer above.**

**Not counted as a pass.** The 7 green runs are reported as 7 runs, not as "the
lane passes", and the 4 red runs are reported rather than hidden. The `-p
no:randomly` figure in §7 is the honest statement of the lane's state: every
test in it passes in a fixed order, and one of them is order-dependent by
construction.

### 8. Not implemented, stated plainly

- **The four mint sites are NOT collapsed.** That is the finding, and
  `MINT_SITES_EXPECTED = 4` + the doctrine-gap pin make it impossible to forget
  and impossible to change silently. Collapsing them means choosing one
  condition for three engines with different evidence shapes — a design decision
  with a Change Log entry, not a side effect of a pin round.
- **The two legacy divergences are NOT fixed.** `legacy.py` still defaults a
  missing `regression_passed` to `True` and still reads an error-bearing evidence
  block as clean. Both are recorded as EXACT facts with inverted pins. Wave 2
  forbids implementing a recorded gap, and these are the two the round's own
  brief calls out.
- **`harness/tools.py::_precommit_refusal` still leaks the gate receipt's
  `context`** into its `ToolResult`. Recorded, owned, pinned open (§2).
- **`harness/test_config.py` is still a production module named `test_*.py`**
  (§4). Recorded, owned, pinned open.
- **`harness/approver.py` still calls `redact_text`/`redact_secrets` directly**
  rather than through `harness/redaction.py`. This is a THIRD direct-caller site
  the Wave-1 pin `test_neither_journal_calls_the_redactor_directly` does not
  cover, because that test only reads `trace.py` and `agent_kernel/events.py`.
  `approver.py`'s journal is AGT-07's separate redacted JSONL and is correct
  today, so it is not in `RECORDED_GAPS` — but it is the obvious next candidate,
  and the Wave-2 scan does not flag it only because its receiver (`append_journal`)
  is not in `JOURNAL_RECEIVERS`. **Recorded here rather than left for the next
  reader to rediscover.**
- **No CI wiring.** `harness/**-local` test modules need a selection entry; that
  is a T5 handoff (§11 of the Handoff), not something this round edited
  (`.github/**` is outside `harness/`).
- **No presentation-path redaction, no `shared/security.py` change, no
  `scripts/lint_ratchet.py` read.** Unchanged from Wave 1.

### 9. The one incidental edit outside the four new files

`harness/AGENTS.md:6732` had `extensions.skill_policy.resolve_permissions / ` with
a **trailing space**, and the next line read `esolve_tools /` — a mangled `r` from
another round's edit (VEX-CS-08). `git diff --check -- harness/` returned exit 2
on it. Repaired both (trailing space removed, `esolve_tools` →
`resolve_tools`), which brings the harness-scoped check to exit 0. Disclosed
because it is not one of the four new files and because it is a text repair in
another round's prose.

### 10. Cross-terminal requests

1. **T5 — CI wiring for four `harness/`-local test modules.** They are inside
   `harness/`, so `pytest tests/…` (the current selection) never runs them.
   Exact command for the selection:
   `python -m pytest harness/test_egress_call_sites.py harness/test_mint_site_pins.py harness/test_gap_audit.py harness/test_structural_claims.py -q`
   (50 passed, 8.5 s, host-only). `harness/test_config.py` must **not** be added
   to any selection until §4 is fixed — it errors on collection today.
2. **T5 / `tests/test_config_trace_state.py`:** no change needed, but note it is
   what caught the BOM (§7). If it is ever weakened to skip `harness/test_*.py`,
   a BOM in a harness test file becomes invisible again.
3. **T5 — `tests/test_ceiling_r2_03_config_guard.py`, jointly with T1:**
   `harness/test_config.py`'s collection error (§4). Seven call sites at
   `:150, :169, :581, :596, :663, :677, :693`.
4. **T1 (next round) — `harness/tools.py::_precommit_refusal`:** one line, route
   `receipt['context']` through `redact_text_for_journal` as
   `lint.render_check` already does. Then delete the `RECORDED_GAPS` entry; the
   inverted pin will fail until you do.
5. **T1 (next round) — `harness/agent_kernel/legacy.py`:** the recorded
   `clean` condition should also require the absence of an `error` (divergence
   #1). Tightening, not widening — but a behaviour change on the legacy path.
   Update `harness/test_mint_site_pins.py`'s two inverted assertions in the same
   change.
6. **T1 / kernel owner — `harness/approver.py`:** route its journal through
   `harness/redaction.py` like `trace.py` and `events.py` do, or add a written
   reason for being the third direct caller (§8).
7. **T5 — `tests/test_agent_loop_matrix.py`: an order-dependent test bug, with a
   reproducer (§7a).** `_clash()` at `:441` is called ONCE at `MATRIX`
   construction (`:568`), so its `state` closure is shared by the four tests that
   parametrize on `tool_failure`; the first to run consumes the failure and the
   rest see `ok=True` and no `reflection` event. Fix: make the tool table a
   factory. **This is why the `test_agent_kernel + test_agent_loop_matrix` lane
   is red in ~4 of 6 randomly-seeded runs and green in 4/4 with
   `-p no:randomly`** — the lane is order-dependent, not flaky, and the fact
   that `-p no:randomly` hides it is worth knowing before anyone attributes it
   to host load.

## P0-W1-T1 — the trust story leaks from the harness side, and four sites mint the gate

**Round:** P0 Foundation, Wave 1, T1 (harness). **Did NOT edit** `cli/**`,
`execution/**`, `runtime/**`, `shared/**`, `memory/**`, `evals/**`, `tests/**`,
`.github/**`, or `scripts/**`.

### 0. Read this first — four mint sites, not one

**`phases/DOCTRINE.md` §2 and `phases/README.md` standing-invariant #1 both
state that `completed_verified` is minted at exactly ONE place. It is minted at
FOUR.** Every one of the four is reachable, and this round changed none of them
— changing a mint *condition* is explicitly out of scope.

| # | site | condition | verdict |
|---|---|---|---|
| 1 | `harness/agent_kernel/completion.py:175` | `target_passed ∧ regression_passed (default **True**) ∧ ¬flaky ∧ ¬error` | strictest |
| 2 | `harness/agent_kernel/verified.py:148-150` | `target_passed ∧ regression_passed ∧ ¬flaky` — **no `¬error` term** | looser |
| 3 | `harness/agent_kernel/legacy.py:114-121` | `target ∧ regression (**default True**) ∧ ¬flaky` — **no `¬error`** | loosest |
| 4 | `harness/agent_loop_step.py:896-901` → `_evidence_is_clean` (`:1126`) | `¬error ∧ target ∧ regression ∧ ¬flaky`, absent term = False | strict, same as #1 |

**Two known divergences, both now pinned as EXACT rather than described:**
`legacy.py` defaults a MISSING `regression_passed` to `True` where the pure
core treats absent as `False`; `verified.py` and `legacy.py` both read a
`target ∧ regression ∧ ¬flaky` block that ALSO carries an `error` as clean,
where the pure core requires the error absent. A verifier that crashed and left
stale booleans behind must not mint a pass.

`harness/test_completion_mint_sites.py` classifies every occurrence in
`harness/**` **by AST** (a grep cannot tell a mint from a mapping), publishes the
enumeration via `enumeration_table()`, and asserts:

- the mint count is `MINT_SITES_EXPECTED = 4` and the mint *file set* is the
  four named above — so a fifth site fails immediately AND a legitimate
  four-into-one collapse fails until the constant moves in the same change;
- `agent_loop_step.py` has **no** `ast.Constant` equal to `"success"`;
- `RUN_STATUSES` contains no bare `success`;
- every `TaskResult(...)` construction in `core.py` passes `verification` as a
  `VerificationResult(...)` call or a `Name` — never a literal;
- the two MAPPING sites cannot widen: `core.py`'s dict maps
  `COMPLETED_VERIFIED.value → "success"` and `success` appears in it exactly
  once; `agent_loop._compat_status` returns `"success"` from exactly one branch.

### 1. One mapping WAS tightened — `_compat_status` could launder a success

`harness/agent_loop.py:1174` used to end `return legacy_status or "failed"`, so
**any** off-vocabulary kernel status returned the LEGACY status verbatim — and
the legacy vocabulary contains the word `success`. `legacy.py` cannot produce an
off-vocabulary status today, so this was a structural hole rather than a live
defect; it is closed because a hole in a truthfulness path is worth closing even
when nothing can walk through it. `"success"` is now returned from exactly one
branch. **This does not widen `completed_verified`; it is strictly a tightening,
and it changed no mint condition.**

### 2. `harness/redaction.py` — the one fail-closed journal boundary (NEW)

**It does NOT implement redaction.** `shared.security.redact_text` /
`redact_secrets` are the authority and were not touched. What the new module
owns is the part the authority cannot:

- **Fail closed.** Redactor raises, or answers `None` for a non-`None` value →
  the value is REPLACED with `(detail withheld: <reason>)`, never passed through.
  Same behaviour and same wording as `cli/notify._redact`, deliberately.
- **Bounded input.** Every string is capped at **`JOURNAL_TEXT_CAP = 200_000`**
  BEFORE the authority runs, keeping the HEAD (the head is where a secret's
  prefix is; a tail-only cap would blind the redactor) and appending
  `[journal value truncated: K of N chars]`. `cap <= 0` means "no cap".
- **One helper, not scattered calls.** `harness/trace.py` and
  `harness/agent_kernel/events.py` are the only two files in `harness/` that may
  call a redactor, and both go through this module. Pinned at the source level
  by `test_neither_journal_calls_the_redactor_directly`.
- `RedactionBoundaryReport` / `journal_redaction_report()` exist because a
  boundary that silently passed raw text would report the same counters as one
  that redacted.

### 3. The redaction boundary decision table

| path | file | decision | why |
|---|---|---|---|
| tool result payload | `harness/agent_kernel/tools.py` `ToolResult`, `harness/tools.py` `ToolBatchStep` | **REDACT AT THE JOURNAL** (already wired; declaration added) | two consumers with opposite trust needs — the model needs the bytes verbatim, every human-facing surface needs the authority. The boundary separates them, so redaction lives where the value leaves the process. |
| journal rows | `harness/trace.py` `log` / `write_receipt`, `harness/agent_kernel/events.py` `append` | **REDACT AT THE BOUNDARY** (wired this round) | preferred option: redacted once where it enters the journal, so every downstream consumer is safe by construction. `receipt.json` is a SEPARATE file with its own call. |
| edit results | `harness/editor.py` `EditOutcome.to_dict`, `EditCandidate.to_dict` | **REDACT AT THE BOUNDARY** (wired) | the ONE place an outcome becomes a record; `candidates[].preview` is real file bytes and `check_context` is real source, and a `.env` edit showed the line. Line numbers, offsets and sha256s are NOT redacted, so the ambiguous-match contract stays exact. |
| error classification | `harness/tool_errors.py` `ToolError.__new__`, `ModelFailure.__new__` | **REDACT AT CONSTRUCTION** (wired) | ~30 construction sites for `ToolError`; a factory is a convention and a convention is what "one authority per concern" warns about. `ModelFailure`'s docstring claimed "never a credential" with **nothing behind it** — `detail` is built from `str(exc)` and a provider exception routinely quotes an `Authorization` header. `kind`/`retryable`/`status_code`/`backoff_s`/`terminal` are untouched: the retry ladder reads them. |
| lint / typecheck | `harness/lint.py` `render_findings`, `render_check` | **REDACT AT THE BOUNDARY** (wired) | a finding's message quotes the offending source token; three consumers with three lifetimes. `file`/`line` not redacted, so findings stay locatable. |
| test failure feedback | `harness/context_compiler.py` `_redact` / `_as_text` | **REDACT AT THE BOUNDARY** (wired; see §4) | one place repository / instruction / skill / memory content becomes model text. |
| prompt templates | `harness/prompts.py` | **DECLARED SAFE, with a reason** | holds no file content, no subprocess output, no env value. Its arguments arrive already-redacted from `context_compiler._as_text` and reach the journal through the two boundaries above. |
| lint baseline / ratchet | `scripts/lint_ratchet.py` | **OUT OF SCOPE — not edited, not covered** | `scripts/` is nobody's slot in this wave. It reads source lines. Recorded in `harness/PARALLEL_HAZARD.md` §5 and in the Handoff. |

### 4. A second redactor was found in `harness/` — and it failed OPEN

`harness/context_compiler.py::_redact_large` reached into `shared.security`'s
**private** compiled patterns (`_QUOTED_SECRET`, `_KEY_VALUE_SECRET`,
`_SECRET_PATTERNS`, `_URL_QUERY_SECRET`, `_CLI_SECRET`, `_redact_explicit`) and,
on any exception, fell back to a local `re.sub` for a `bearer` prefix alone —
a pattern set narrow enough to let an `sk-...` key straight through. `_redact`
had the same fail-open `except`, plus a 16 KiB **marker gate that returned the
RAW string on a marker miss**: a ten-alternative regex is not a secret
detector.

Both are now thin delegates to `harness.redaction.redact_text_for_journal`
(fail-closed). `_redact_large` is kept only so a caller's import keeps working.

**The fast path was measured and is no longer needed.** The authority is linear
because it bounds its scan span (`REDACTION_SCAN_SPAN_CAP = 4096`):

| payload | time |
|---|---|
| 16 kB prose | 0.008 s |
| 65 kB prose | 0.046 s |
| 200 kB prose | 0.153 s |
| 1 MB prose | 0.886 s |
| `"y" * 1 000 000` | 0.817 s |

Linear in length AND in character class, so the "unbounded URL matcher"
justification no longer holds. Pinned by `test_the_authority_is_linear_across_a_ten_fold_length_range`.

`test_the_context_compiler_does_not_keep_a_second_redactor` now fails if any
`harness/` file reaches into `security._` or declares `REDACTED]` — with the
documented `trace.LEGACY_REDACTED` compat constant exempted **by name** so the
exemption is visible rather than a loosened pattern.

### 5. A regression this round's own first draft shipped, and what caught it

`redact_for_journal`'s first version recursed into leaves and applied the
authority to each string **separately**. That destroys the key→value
relationship `redact_secrets(value, key)` depends on: `{"api_key": "top-secret"}`
came out with `top-secret` **intact**, while every test using a bare
`sk-…`-shaped string still passed.

**Caught by `tests/test_config_trace_state.py::test_trace_redacts_nested_credentials`**
(T5's file) on the first required-suite run. The fix is two passes: `_cap_tree`
bounds the input preserving structure, then the **whole** capped structure goes
to the authority once. Both halves are now pinned harness-side
(`test_a_credential_is_still_redacted_under_the_key_that_names_it` and
`test_a_key_named_credential_is_redacted_even_when_the_value_is_ordinary`).

### 6. The three redactor measurements (T1.W1.2) — the fix IS present and IS reached

```
redact_text("y"*40000)                  0.025 s
redact_text("a"*200000)                 0.136 s
redact_text(("sk-"+"A"*39+"\n")*500)    0.012 s
```

`shared/security.py` carries `REDACTION_SCAN_SPAN_CAP = 4096`,
`REDACTION_SCAN_SPAN_OVERLAP = 512`, `_LONG_LINE`, `_REPEATED_RUN` and
`_prescan_redaction` — the fix landed. Reachability from the harness journal is
pinned by `harness/test_redaction_boundary.py` (12 tests: the authority, the
ratio, all three named measurements, both journal paths writing redacted rows,
the cap firing before the authority, head retention, `cap=0`, the counters, and
the non-vacuity control that ordinary values are untouched).

### 7. `harness/PARALLEL_HAZARD.md` — the cross-slot map (NEW)

Complete cross-slot import table (every module of `harness/` another slot
imports, with the importing file), a hazard ranking derived from which phase
prompts name which harness files, and four concrete narrowing rules:

- **`config.py`** — phase-scoped regions, appended not sorted; `None` unless the
  prompt names a value.
- **`prompts.py`** — a new named constant + one changed reference + a
  `python -m evals.run` run. Never edit a prompt string in place (AHEAD
  measured prose alone at −2.3pp).
- **`tools.py`** — append catalog entries only; editing an existing one changes
  `catalog_fingerprint`.
- **the journal authorities** — call `harness/redaction` from the existing emit
  site; a third call site is a defect.

**Observed live in this very wave** (recorded because it is the hazard, not a
hypothetical): `execution/ingress.py` is a NEW untracked file, and mid-run it was
half-written with a `SyntaxError` at line 259. That broke `import execution`,
which made `tests/test_agent_kernel.py::test_daily_kernel_routes_mutations_through_safe_workspace_backend`
fail with a missing `execution_backend_ready` row and
`::test_default_shell_handler_honors_a_cancelled_token` fail on the import. Both
passed on re-run once T2's write completed. **Neither was repaired or counted as
a pass in the run where it failed.**

### 8. Verification actually run (this tree, `-p no:randomly`)

- **NEW `harness/test_secret_egress.py` → 25 passed** (the 12 redaction-boundary
  tests plus the 13 path/declaration tests, counting the parametrised journal
  pair).
- **NEW `harness/test_redaction_boundary.py` → 12 passed.**
- **NEW `harness/test_completion_mint_sites.py` → 11 passed.**
- `tests/test_agent_kernel.py tests/test_tool_protocol.py tests/test_config_trace_state.py`
  → **90 passed**, exit 0.
- `tests/test_agent_loop.py tests/test_verification_gate_wiring.py` → **89 passed**, exit 0.
- `python -m evals.run --check` → **14/14 CLEAN**, exit 0.
- `python -m compileall -q harness` → clean.
- `python -m ruff check harness` → **6 findings, all pre-existing in files this
  round did not touch** (`_stubs/model_router.py`, `agent_kernel/kernel.py`,
  `docs_lookup.py`). Proven, not assumed: the two tracked files were piped from
  `git show HEAD:<path>` into ruff and reported the same findings, and
  `git status` shows both are unmodified against HEAD.
- **NO Docker lane and NO live-provider lane were run, and neither is claimed.**
  Every model call in the new suites is a scripted double. The required lanes
  above are host-only.

### 9. Not implemented, stated plainly

- **The four mint sites are NOT collapsed.** That is the finding, and collapsing
  them means choosing one condition for three engines with different evidence
  shapes — a design decision with a Change Log entry, not a side effect of an
  audit. `MINT_SITES_EXPECTED = 4` makes the gap impossible to forget and
  impossible to change silently.
- **`scripts/lint_ratchet.py` was not read for leaks or fixed.** It is outside
  `harness/` and outside every slot in this wave.
- **`shared/security.py` was not touched**, and the private-name coupling that
  used to exist is now recorded as a request to T5 rather than as a second copy
  of its patterns.
- **No presentation-path redaction in `harness/`.** `cli/ui.py` ordering
  (`strip_ansi` before `redact_text`) is T4's, and this round only ensures the
  journal never hands T4 an unredacted value to reorder.
- **The `harness/approver.py` journal is unchanged** (AGT-07's separate
  redacted JSONL, which is already correct and unwired).
- **No `evals/`-side fix for the `status == "success"` probes.** That is T5's;
  the harness-side enumeration is what they need.

### 10. Cross-terminal requests

1. **T5 / `shared/security.py`: expose a documented bounded redaction entry
   point.** `harness/context_compiler.py` used to reach into five private
   compiled patterns (`_QUOTED_SECRET`, `_KEY_VALUE_SECRET`, `_SECRET_PATTERNS`,
   `_URL_QUERY_SECRET`, `_CLI_SECRET`) plus `_redact_explicit`. That coupling is
   gone, but the underlying need is real: a consumer wanted "the whole policy
   without the unbounded URL matcher". A public
   `redact_text_bounded(value, *, scan_span_cap=...)` would remove the last
   reason any consumer has to touch a private name.
2. **T5 / `tests/test_config_trace_state.py`: no change needed, but read
   `test_trace_redacts_nested_credentials`.** It is the test that caught this
   round's own regression (§5). If it is ever weakened to a leaf-only assertion
   the redaction boundary will quietly stop working.
3. **T5 / `evals/daily_driver.py`: the ten `result.status == "success"` probe
   sites.** AGT-02/03/04/05/07/11 each recorded this. The harness-side
   enumeration is in §0 above; `completed_verified` is the only honest value a
   completed run can carry, and the four mint conditions are now written down.
4. **T4 / `cli/ui.py`: nothing to do for this round, and that is the point.**
   The journal hands presentation a value that is already redacted. What T4
   still owns is the `strip_ansi`-before-`redact_text` ordering, which lets ANSI
   escapes reassemble a split secret into visually-contiguous bytes — that is a
   presentation-order defect and cannot be fixed from `harness/`.
5. **T2 / `execution/ingress.py`: a file the whole tree depends on.** It was
   unparseable for ~4 minutes mid-write during this round's first required-suite
   run, which took `test_agent_kernel.py` red with two failures that had nothing
   to do with either of them. Consider an import-time smoke test or an atomic
   write for new modules, since a half-written file in a shared worktree reads
   as a regression in whatever suite ran at that moment.

## AGT-11 - the loop is one pure function, and the matrix that pins it

**Owns: NEW `harness/agent_loop_step.py`, the AGT-11 region of
`harness/agent_loop.py` (the reply-parser re-export, `STEPPED_STRATEGY`,
`HarnessToolbox`, `run_agent_stepped`, `_evidence_is_clean`), and NEW
`tests/test_agent_loop_matrix.py`. Did NOT touch `harness/core.py`,
`harness/agent_kernel/**`, `harness/config.py`, `execution/**`, any verifier,
or the `_run_agent_legacy` loop body - it is byte-identical. **The verifier
gate is untouched and is structurally unreachable from the new code**: the
terminal vocabulary IS `shared.agent_contracts.RUN_STATUSES`, which contains
no bare "the run worked" word, and `completed_verified` has exactly one mint
site. Nothing here replaces verification with prompt shaping.**

The problem: AGT-G21. The loop's *decisions* and its *effects* were interleaved
in one 1,255-line function, so the only way to ask "what would the loop do
here?" was to run it against a real repository, a real sandbox, a real
provider and a real clock. Fourteen important behaviours each cost a
subprocess to observe once. OpenHands' insight is worth taking; its
infrastructure is not.

### 1. One pure function, and how the effects get in

```python
step(history, tools, config) -> events
```

A total function of its three arguments. No I/O, no clock, no globals, no
randomness. `tools` is the ENTIRE injected boundary - the model, every tool,
the elapsed-seconds reading, the spend meter, the cancel flag, the approver
and the steering inbox - and `LoopEnvironment`'s defaults are the load-bearing
part: **`approve()` returns `False`**, so a `require`-mode run with no
approver refuses rather than proceeding silently. That is the same fail-closed
answer the legacy loop reaches, and putting it in the default is what makes
it un-forgettable.

`step` runs the loop **to its terminal event** and returns the whole event
list, so the adapter's entire persistence job is a `for` loop of
`trace.log(kind, data)`. Exactly one `task_end`, always last; every kind is in
the closed set `EVENT_KINDS`.

Purity is pinned four ways, because a comment is not evidence:

| pin | what it catches |
|---|---|
| import pin | a direct `time`/`os`/`socket`/`subprocess`/... import |
| module-global pin | a dict/list/set literal the loop could read |
| determinism pin | the two disagreeing |
| **poisoned clock + poisoned socket** | a transitive helper quietly reading either |

The poisoned-clock test is the one that earns its place. Source scans and
determinism both pass on a function that calls `time.time()` and happens to be
deterministic within a test run; raising from the clock and the socket during a
whole run does not.

### 2. The reply grammar moved, and was proven to move unchanged

`parse_tool_call`, `_TOOL_NAMES`, `_PLAIN_PATTERNS` and `_strip_fences` now
live in `agent_loop_step.py` and are **re-exported** from `agent_loop`, so
`from harness.agent_loop import parse_tool_call` (used by
`tests/test_agent_loop.py`) keeps working untouched. The parser is a pure
function of one string; it belongs beside the function that calls it.

A differential against the pre-move implementation - **35 hand-written replies
x two verb sets, plus every string of length <= 3 over a grammar-shaped
alphabet** - reported **0 divergences**. Recorded in the Change Log so the
next reader does not have to re-derive it.

### 3. A bug this round's own suite found in its first draft

The first draft stopped the run on `not reflection.allowed`. **That is the
wrong flag**, and it took the whole matrix to notice: `allowed=False` also
means "this is a refusal, do not invite a retry", so the loop ended `failed`
on the first policy refusal, the first approver denial and the first
environment fault. A user saying no became a failed task.

The correct predicate is `not reflection.exhausted` - a CAP is the only thing
that stops the run, which is exactly what AGT-02's `_reflect` already did. A
refusal is now reported, the loop CONTINUES (the right next move is a
different one), and it still spends no budget. `test_a_refusal_is_never_
charged_against_the_recovery_budget` asserts the free arm **and** the charged
arm, so it cannot pass by never charging anything.

### 4. The verifier gate, and two recorded divergences

`completed_verified` is reachable from exactly one place: a declared verifier
returned clean evidence. A run with no declared tests is
`completed_unverified` plus a `verify_skipped` event that names itself. A
source-level pin fails the build if `agent_loop_step.py` ever contains a string
literal equal to `success` outside its own docstrings.

`test_the_three_mint_conditions_agree_across_the_tree` found a real one. There
are now three copies of "what counts as clean evidence", and **the legacy one
is looser in two ways**:

1. it reads only the three booleans, so an evidence block that ALSO carries
   an `error` is "clean" to it. The pure core requires the error to be absent.
2. it defaults a MISSING `regression_passed` to `True`; the pure core treats an
   absent term as `False`.

The pure core is the strict side in both, and both are asserted EXACTLY rather
than papered over. **Divergence 1 is a candidate fix, not a description**: a
verifier that crashed and left stale booleans behind must not mint a pass. It
is filed below rather than applied, because `harness/agent_kernel/legacy.py` is
not this round's file.

### 5. Two adapter bugs this round found and pinned

- `_run_verify` emits its own `verify` row for the legacy loop's benefit, and
  the core emits the canonical one from the same evidence. Both landed: **two
  journal rows for one verifier run** in the authoritative journal. The adapter
  now hands it a no-op emit, and the matrix asserts `len(verify_rows) == 1`.
- The executor's changed-file delta was never handed to the core, so the
  terminal receipt's `files_touched` was **empty on the real path** while the
  result carried the list - journal and result disagreeing about what changed.
  The delta is now attached to the `ToolOutcome` detail, and the two are
  asserted equal.

Both were invisible to a pure-function test and visible only because the matrix
runs every scenario through the real adapter too.

### 6. The matrix, run twice per scenario

Fourteen scenarios, in a table that is asserted to BE the required table:
question, research, build, fix, multi-file, tool failure, policy refusal,
context exhaustion, resume, cancel, doom-loop, unparseable tool call, approver
denial, network unavailable. Each runs **twice** - against the pure function
with a scripted boundary, and through the real `run_agent_stepped` on a real
repository with a real journal, the real model boundary, the real executor and
the real verifier seam. The two statuses must AGREE, which is what proves the
adapter contains no decision. Every case also asserts the events, that the run
stopped with a reason, and that it never dressed an unverified completion as a
pass.

### 7. The UXP corpus, executed - and the gaps, recorded

Seven phrasings that broke in real use, transcribed with their ORIGINAL
incident notes from the classifier tables, are cases: classified per-sentence,
run through the real adapter, and asserted never to mutate a repository on
their own (the negative control that makes the sweep non-vacuous).

Three further phrasings the classifier does NOT route today - a bare
specification clause, a question with no interrogative word, non-English
work-shaped input - are recorded as `KNOWN_CLASSIFICATION_GAPS` with an
**inverted** pin that FAILS the day someone fixes one, telling them to promote
it into the corpus. A recorded gap is one somebody can close; an unrecorded one
just gets rediscovered. I did not widen `classify_deterministic` to make them
pass: that is a classifier round, not this one, and it would have changed
behaviour the existing classification matrix pins.

### 8. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_agent_loop_matrix.py` -> 95 passed, 2 skipped.** Host-only:
  no Docker, no provider, no network. The 2 skips are the `cancel` row's
  real-adapter runs - cancellation is driven by a real in-flight interrupt, so
  it is proven by `test_agt_10_batch_boundary.py` and `test_steering.py` rather
  than faked here, and the skip says so.
- `test_agent_loop` + `test_agt_02_reflection` + `test_agt_10_batch_boundary` +
  `test_config_trace_state` -> **121 passed**. These four own the parser move
  and the AGT-02/AGT-10 regions, so they are the ones that matter here.
- `test_agent_kernel` + `test_tool_protocol` + `test_verification_gate_wiring` +
  `test_agent_loop_matrix` -> 103 passed, 1 failed.
- `test_modes` + `test_cli_runview` + `test_cli_tracelog` +
  `test_ceiling_r2_04_daily_default` -> **258 passed, 2 skipped**.
- `test_capability_surface` + `test_cli_command_system` +
  `test_ceiling_r2_03_config_guard` + `test_stubs_and_deps` -> **216 passed,
  1 skipped**.
- `test_steering` + `test_recovery_steering` -> 101 passed, 7 skipped, 1
  failed.
- `test_evals_run` + `test_evals_tasks` + `test_daily_driver_evals` -> 67
  passed, 2 failed.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0.
- `ruff check` clean on all three owned files. The two NEW files are
  `ruff format` clean; `harness/agent_loop.py` keeps its pre-existing
  whole-file format debt (parallel terminals' regions) and **my own hunks were
  hand-formatted** rather than running the formatter over a shared dirty file.
  `compileall` clean.

**Three failures, none mine, none counted as passes, none weakened:**

1. `test_verification_gate_wiring.py::test_a_run_with_no_intelligence_config_is_byte_identical`
   and 2. `test_recovery_steering.py::test_two_502s_recover_through_the_real_fix_loop`
   are both `execution.sandbox.SandboxUnavailableError: docker daemon not
   reachable`. **The Docker daemon is not running on this host** (`docker
   version` fails to connect to `dockerDesktopLinuxEngine`). Blocked lanes, not
   passes. Both drive `harness/core.py` / the real verifier, which this round
   does not touch.
3. The two `test_daily_driver_evals.py` failures
   (`test_feature_evidence_lane_covers_every_active_feature`,
   `test_quick_matrix_runs_real_comparison_with_receipts`) are the ones AGT-03,
   AGT-04, AGT-05 and AGT-07 each recorded: the probes assert
   `status == "success"` against the honest `completed_verified` on the legacy
   `harness.core.run_task` path. **Proven not mine, not assumed:** importing
   `harness.core` in a fresh interpreter loads **no** `harness.agent_loop*`
   module (verified by import probe), and `agent_step` appears in no config key,
   default or eval arm anywhere in the tree.

**No Docker lane and no live-provider lane were run and neither is claimed.**

### 9. Not implemented, stated plainly

- **`harness/agent_loop.py` is not yet "adapters only".** The NEW code in it
  is adapters only; the old 1,255-line `_run_agent_legacy` is still there,
  byte-identical, and `agent_step` is an **explicit pin that is never a
  default**. This was a deliberate, asked-for scope decision: deleting that
  loop means re-implementing AGT-02's reflection, AGT-10's batch-boundary
  steering, the doom-loop guard and the DONE-branch verifier mint inside the new
  core, against ~100 existing tests and the release gate. The remaining step is
  to route `legacy_agent` through `step` incrementally, one seam at a time, with
  this matrix green after every increment.
- **Making `agent_step` the default is unmeasured.** The matrix proves the two
  engines agree on 14 situations; it says nothing about model quality, cost or
  wall clock on a real task. Anyone flipping the default needs
  `python -m evals.run --suite daily-driver` first.
- **The mint divergence in §4 is filed, not fixed.** `legacy.py` should also
  require the absence of an `error`; that file is not this round's.
- **The pure core has no plan phase, no self-critique, no agent-written
  edge-case tests, and no subagents.** Those are `harness/core.py` and
  `harness/agent_kernel/strategy.py` capabilities. The stepped engine is a
  smaller engine on purpose - a decision function - and inheriting them is a
  separate piece of work.
- **`cli/` was not edited.** `run_agent` dispatches to `run_agent_stepped` when
  `agent_strategy="agent_step"`, and nothing in `cli/` sets it, so the REPL and
  TUI behaviour is unchanged. Wiring the pin to a CLI flag is a CLI-owner
  integration.
- **Three new trace kinds are unmapped in `cli/runview.py`:** `policy_refused`,
  `environment_unavailable` and `steering_delivered`. The same `EVENT_VOCABULARY`
  gap AGT-02 and AGT-10 both filed; one more file for the same fix.
- **The three `KNOWN_CLASSIFICATION_GAPS` are still gaps.** See §7.

### 10. Cross-terminal requests

1. **`harness/agent_kernel/legacy.py` (kernel owner): one line, and it is a
   tightening.** `clean` should also require the absence of an `error`, matching
   the new core: a verifier that crashed and left stale booleans must not mint
   a pass. `tests/test_agent_loop_matrix.py::
   test_the_three_mint_conditions_agree_across_the_tree` asserts the current
   divergence EXACTLY and will need updating in the same change.
2. **`cli/runview.py` owner - three more unmapped kinds.** Add
   `policy_refused`, `environment_unavailable` and `steering_delivered` to
   `EVENT_VOCABULAR` (or `INFORMATIONAL_EVENTS` for the receipt-shaped ones).
   This is the third round to file the same one-line fix.
3. **`cli/interactive.py` / `cli/tui.py` owners:** a flag for
   `agent_strategy="agent_step"` is the whole integration. Do not make it the
   default without running the daily-driver matrix.
4. **`harness/intent.py` / `classify_deterministic` owner:** the three
   `KNOWN_CLASSIFICATION_GAPS` in `tests/test_agent_loop_matrix.py` are
   ready-made failing cases, and the inverted pins will tell you when you have
   fixed one.

## AGT-05 - plan-phase research isolation (2026-09-29)

**Files owned/edited:** `harness/agent_kernel/subagents.py` (NEW public
surface, same file as the bounded `task` spawn), `harness/agent_kernel/strategy.py`
(the plan seam + ONE pre-existing defect fix), `runtime/roles.py` (the planner
profile now derives from the gate), `harness/config.py` (seven `None` keys),
NEW `tests/test_agt_05_plan_isolation.py`. **Disclosed edit outside the file
list:** one vacuous gate corrected in
`tests/test_ceiling05_knowledge.py` (§6 - it was passing because a run was
crashing, and my change is what surfaced it). `harness/core.py`,
`harness/agent_loop.py`, `harness/prompts.py`, `harness/completion.py`,
`harness/tools.py` and every verifier were NOT edited. **The verifier gate is
untouched**: nothing here can make `completed_unverified` reachable as
success, and nothing here replaces verification with prompt shaping - the
plan phase is a bounded read-only subagent plus a receipt, and no plan prompt
is ever a substitute for a verifier run.

The problem: a plan phase is worth having only if the exploration it does
never enters the main window. Prompting for that does not work, because the
mechanism that makes the paste easy is the same one that makes the answer
possible. So the isolation is structural.

### 1. The researcher has its own context, and the type cannot leak

`PlanResearchSubagent` (`harness/agent_kernel/subagents.py`) is a small
explicit loop, not a nested kernel run, because the three properties that
matter are all properties of *this object*:

* **Own context.** `self._messages` is built inside it and never handed out.
* **Own return type.** `PlanResearchResult` has no field capable of holding a
  transcript - `plan`, `citations`, and counters only. That is why
  `_render_plan_block` cannot leak exploration text: the text is not there to
  render. The assertion is on the SHAPE
  (`test_the_receipt_carries_no_field_that_could_hold_a_transcript` pins the
  exact key set), because a future field could otherwise reintroduce the leak
  silently.
* **Still honest about what it did.** `transcript_messages`,
  `transcript_chars` and `transcript_digest` report the researcher's own
  window, so "the isolation worked" is measurable and not merely claimed. The
  leak test is paired with a control that proves the researcher really did read
  the file - otherwise "the marker is absent" would pass vacuously.

The plan is injected as the FIRST piece of `_metadata_context`, so it is part
of the `[system, user]` frame that is seeded once and is then a prefix of every
later request. The plan phase runs **before** the first base-frame build;
running it after the seed would mean a plan injected into a conversation that
had already started, which is the shape this requirement forbids.

### 2. Plan mode is a capability gate, not an instruction

```python
PLAN_RESEARCH_CAPABILITIES = ("read", "control")
PLAN_RESEARCH_WITHHELD_TOOLS = ("task",)
```

`plan_mode_tools()` is **derived** from the one canonical catalog through
`kernel.capability_surface()`. There is no hand-written list to forget to
update. The researcher's `ToolRegistry` is `restrict()`ed to that set *before*
any handler is installed, so a write is **not a tool the researcher has**:
`ToolRegistry.validate` raises `unknown tool` before a handler could run. That
is a stronger property than a policy denial - a policy can be configured
open, a missing tool cannot.

`control` is granted deliberately: a researcher that could not end its own turn
could not return a plan at all. The one exception is named - `task`, because a
researcher that can fork work has an unbounded budget by construction.
`plan_mode_withheld()` returns each withheld capability with its reason AND the
tools it carried, so the receipt answers "what exactly is unreachable".

`runtime/roles.py`'s `planner` profile now derives `visible_tools` from the
SAME function (`_planner_tools()`). Its previous hand-written list was a
strict subset that could drift in both directions: a newly added read tool was
invisible to a planner, and a renamed tool broke it. It also gained
`planner_capability_gate()` and `planner_withheld_reason()` as the role-layer
view of the same receipt.

**Read this before "fixing" the gate by effect class.** The gate follows
CAPABILITIES, not effect classes. `ask` and `finish` are `control`-class tools
and are reachable; a `workspace_write` tool never is. A test that asserts
"non-read_only implies unreachable" encodes the wrong rule and fails on the two
tools the mechanism needs.

### 3. The return is bounded on every axis, and says what it dropped

Turns (6), tool calls (12), dollars (0.25, checked BEFORE the next call), the
returned plan's size (4000) and the citation count (12). The receipt names
which bound stopped the run (`stopped_because`).

**The size cap is a hard ceiling, and that took a fix.** The first version
reserved a fixed 32 characters for the omission marker; a six-figure omission
count produced a marker LONGER than that, so the "capped" result was 16
characters over the limit - the exact failure a cap exists to prevent. The
marker is now measured from the real omission count and the head/tail are sized
to fit what remains. Verified over a 3000-case grid (every limit 0-299 against
body sizes from 0 to 200 000) with zero violations. `max_chars = 0` means
"return nothing", a real choice rather than an accidental "no cap".

### 4. The architect/coder split, and both halves reported

`plan_model` binds a SECOND `ModelGateway` for the plan request and nothing
else, reusing the run's own boundary (`call_fn` / `model_client`) exactly as
the compaction summarizer's gateway does - a second gateway rather than a
mutation, because the cheap executor must not inherit the architect's price,
and the shared boundary because a planner that quietly resolved the default
provider would be untestable and unaccounted. **If that boundary is unreachable
the key is NOT honoured** and the receipt names which model actually planned.
The researcher's spend is folded into the run's totals by `_absorb_spend`.

### 5. The approved plan IS the executed plan, or the run is `blocked`

`approve_plan(spec)` compares the digest of the approved plan
(`RunSpec.metadata["plan_guidance"]` - the path the CLI's `/plan` preview
already used) against the digest of the block this run actually injects. A
mismatch returns `completion.blocked(...)` and executes nothing. Approving plan
A and executing plan B is refused, not performed quietly. An absent approved
plan is **not** inferred as an approval: `plan_approval_receipt()` reports
`approved: False`. Both receipts ride `RunResult.metadata["plan"]` and
`["plan_approval"]`, and neither carries any completion vocabulary (pinned by
`test_the_plan_receipt_carries_no_completion_vocabulary`).

### 6. A pre-existing defect, and the vacuous gate that hid it

`knowledge()` memoises `False` as its "unavailable" sentinel but returned that
raw sentinel on the **memo-hit** path, while every caller tests
`knowledge is None`. The plan phase calls `_compile_knowledge`, so a run with
`knowledge_enabled=False` raised `'bool' object has no attribute 'compile'` on
turn one. **Fixed at the one place that owns the sentinel** (`return
self._knowledge or None`) rather than by re-guarding its three call sites;
`_close_knowledge` reads the raw attribute and stays correct.

The defect was invisible because
`test_ceiling05_knowledge.py::test_knowledge_off_arm_never_injects_and_never_compiles`
was **passing vacuously**: it asserted the `AGENTS.md` marker was absent but
never asserted the run's STATUS, so a run that crashed before the model was
ever called satisfied every assertion. The marker was ALSO the wrong
discriminator - `AGENTS.md` is read independently by
`ContextBuilder.discover_project_instructions`, so it is present in BOTH the
on and off arms (measured: the knowledge path uniquely adds `## Compiled
repository context`, `## Repository map`, `### Cited sources` and the symbol
citations; the AGENTS.md marker is not one of them).

That test now asserts a real completion - so a crash can never satisfy it
again - and asserts the absence of the knowledge block's own header. A NEW
control test proves project instructions still reach the model with the
knowledge compiler off, so the fix cannot be "made" by deleting instruction
discovery. **No assertion was weakened**; both are strictly stronger.

Reverting the one-line sentinel fix fails 5 of the new suite's tests, so the
regression cannot return silently.

### 7. Config discipline - nothing behaviour-changing in `DEFAULTS`

All seven keys are `None`: `plan_research`, `plan_model`,
`plan_research_max_turns`, `plan_research_max_tool_calls`,
`plan_research_max_cost_usd`, `plan_research_max_chars`,
`plan_research_max_citations`. `None` is behaviour-neutral because an absent
key already means "use the internal default" and because a value in `DEFAULTS`
is merged into every task and every eval arm - a truthy `plan_research` would
have silently switched every run in the project onto a different architecture
(an extra bounded model call before turn 1). The OFF arm's prompt is
byte-identical to the pre-round one, which is what keeps the prompt-regression
matrix comparable.

`plan_research` is read by key presence AND an explicit truthy
(`"1"/"true"/"yes"/"on"/"research"`), so a typo in a settings file cannot
enable it. Every bound is clamped to a floor of zero and an unusable value
degrades to the default rather than to infinity - an unbounded bound is not a
bound. `test_no_behaviour_changing_default`-style pins: the shipped-default
test and the nonsense-bound test.

### 8. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_agt_05_plan_isolation.py` -> 47 passed.** Host-only: no
  Docker, no provider, no network. All five required proofs are present and
  named: `test_a_write_is_not_a_tool_the_researcher_has` (and the shell
  twin), `test_no_exploration_text_reaches_the_executors_first_request` (with
  its vacuity control), `test_a_very_long_plan_comes_back_bounded_and_says_so`,
  `test_a_configured_plan_model_is_used_for_planning_and_reported`, and
  `test_an_approved_plan_that_is_not_the_executed_plan_is_refused` (with its
  matching-proceeds control). Plus the defence-in-depth write refusal under a
  permissive policy, the byte-identical file after a real write attempt, the
  gate-derivation pin, the three "policy rather than prompt" refusals, the
  six budget bounds, the receipt-shape pin, the OFF arm, the eleven-case switch
  matrix, and four verifier-gate pins.
- Neighbours -> `test_agent_kernel` + `test_ceiling05_knowledge` +
  `test_orchestration` + `test_ceiling06_orchestration` + `test_tool_protocol` +
  `test_workspace_security` + `test_capability_surface` +
  `test_config_trace_state` + `test_agt_04_search_budgets` -> **305 passed, 1
  skipped**; plus `test_agt_02_reflection` + `test_agt_03_edit_lint` in an
  earlier combined run -> **192 passed, 1 skipped**;
  `test_agent_loop` + `test_modes` + `test_retrieval_tools` + `test_lsp` +
  `test_ceiling_r2_04_daily_default` -> **214 passed, 1 skipped**;
  `test_stubs_and_deps` + `test_recovery_steering` + `test_ceiling_r2_15_trust`
  + `test_ceiling05_knowledge` + `test_difficulty_approval` -> **170 passed**;
  `test_evals_run` + `test_evals_tasks` + `test_daily_driver_evals` -> **69
  passed**.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0.
- `python -m evals.run --suite daily-driver --no-docker --json` -> **52/52
  case arms ok, 0 fail**, `zero_false_verified_successes`,
  `zero_unauthorized_mutations`, `zero_lost_edits` all **true** - identical to
  the AGT-02/03/04 recorded baseline, which is the measured answer to "does an
  opt-in extra bounded model call perturb a healthy run". Verdict
  **`NOT_READY`**, reported honestly: the Docker lane, the live-provider lane
  and the sampled manual-repair evidence were **not selected** and are not
  claimed.
- `ruff check` clean on all five owned/edited files; `compileall` clean.
  (`harness/config.py` holds pre-existing whole-file format debt and was not
  swept, per the shared-file protocol.)
- **One red result, measured rather than waved away:**
  `tests/test_orchestration.py::TestOrchestratorSubprocess::test_child_wall_
  timeout_retries_and_resumes` failed ONCE inside a 3-file combined run and
  passed standalone and in two consecutive whole-file runs. It is the host-load
  class `runtime/AGENTS.md` already documents, and the margin was measured: the
  child imports `litellm` in **15.36s** against the test's
  `max_workflow_wallclock_s=15.0`, while AGT-05's wider planner list costs
  **+0.19 ms** of registry construction (0.841 ms vs 0.655 ms for the old hand
  list) against that 15,000 ms budget. A controlled A/B (revert -> 19 passed;
  restore -> 19 passed twice) plus 0.19 ms cannot account for a 15-second miss.
  **Not counted as a pass in the run where it failed, and no assertion was
  weakened.** Details in `runtime/AGENTS.md`.

### 9. Not implemented, stated plainly

- **No live-provider lane was run and no credential was inspected.** Every
  model call in the suite is a scripted boundary. Nothing here is evidence
  about a real planner's plan QUALITY, only about the architecture around it.
- **No Docker lane was run.** Not needed: the plan subagent cannot shell out,
  so there is no sandbox path to exercise.
- **The researcher's prompt is module-local** (`_PLAN_SYSTEM` in
  `subagents.py`), not in `harness/prompts.py`. It is invisible to the
  prompt-regression matrix by construction, which is why the baseline is
  unchanged. If it ever moves to `harness/prompts.py` it must go through
  `python -m evals.run` before shipping, like any other prompt change.
- **The plan subagent is a single in-process loop, not a `task` spawn.** That
  is deliberate (its own context, its own gate, no orchestration budget) but
  it means it does not appear in `runtime.orchestration`'s DAG, has no
  worktree, and cannot be fanned out. A planning phase that wants
  parallel exploration is still unbuilt.
- **The plan block is injected; it is not enforced.** Nothing forces the
  executor to follow the plan, and `plan` control-tool calls mid-run are
  journaled but do not re-open approval. A replan that changes the plan is a
  known gap - the binding is on the INJECTED plan, not on a live
  plan-vs-plan comparison.
- **`cli/` was not edited.** The `/plan` preview still calls
  `harness.agent_loop.render_agent_plan` (a heuristic that reads retrieval
  files and formats steps) rather than the kernel's plan subagent, and
  `/plan` does not yet offer the architect/coder split. That is a CLI-owner
  integration; the harness seam is complete and reported.
- **The `planner` role's tool list WIDENED** (it gains `list`, `image`,
  `git_log`, `git_show`, `git_blame`, `git_branch`, `git_worktree`,
  `read_symbol`, `find_definition`, `find_references`, `blast_radius`, and
  LOSES `memory`). All additions are read-only; the loss removes the memory
  query from a planning role, which is deliberate (a planner records memory
  with `memory_record`, which is a WRITE, and plan mode withholds `memory`).
  `test_orchestration.py` re-run green (19 passed).

### 10. Cross-terminal requests

1. **`cli/interactive.py` / `cli/tui.py` - the `/plan` preview should use the
   kernel's plan subagent.** `_agent_plan_preview` currently calls
   `harness.agent_loop.render_agent_plan`, which is a deterministic heuristic:
   it formats retrieval file names into three canned steps and never explores.
   Wiring `plan_research` into the preview would make the plan the user
   approves the plan that was actually researched - and `approve_plan` is
   already waiting to check exactly that. Both REPL (`interactive.py:6166`) and
   the TUI modal (`tui.py:5947`) are CLI files and were not edited.
2. **`cli/commands.py` - the `plan` MODE profile still has its own hand-written
   `visible_tools`.** `cli/commands.py:726` defines a `ModeSpec` with its own
   tool list, the same drift AGT-05 removed from `runtime.roles`. It should
   call `runtime.roles.planner_capability_gate()` (or
   `harness.agent_kernel.subagents.plan_mode_tools()`) the way the role now
   does. `cli/commands.py` is not this round's file.
3. **`harness/agent_loop.py` - the legacy loop has no plan phase at all.** It
   builds its plan from `render_agent_plan` and injects it as steering
   guidance. The strict kernel now has a real one; the legacy engine is
   unchanged, as with AGT-02/AGT-04.
4. **The replan re-approval gap is a design decision someone should own.** As
   shipped, the plan binding covers the INJECTED plan. If a mid-run `plan` tool
   call is meant to re-open approval, that is a policy decision (does a
   replan pause the run for a human?) and belongs to the CLI owner together
   with request 1.

## AGT-08 - the effort ladder reaches the model boundary, and the cheap tier summarizes (2026-09-28)

**Files owned/edited:** `harness/model_client.py`, `harness/config.py`,
`harness/agent_kernel/strategy.py` (the compaction tier only),
`harness/agent_kernel/checkpoints.py`, `harness/agent_kernel/legacy.py`
and `harness/agent_kernel/verified.py` (one additive kwarg each, for the
resume identity), plus NEW `tests/test_agt_08_effort.py`.
**Did NOT touch** `harness/core.py`, `harness/agent_loop.py`, any verifier,
any completion status, or the success mint. `runtime/**` and `cli/**` are
documented in their own `AGENTS.md`; `INTERFACES.md` carries the full
contract under the 2026-09-28 AGT-08 Change Log entry.

Three harness-side facts, and the full reasoning is in `runtime/AGENTS.md`.

### 1. The boundary rule, and the defect the eval matrix caught

`ModelClient.call` / `call_structured` gained an `effort` keyword, forwarded
ONLY when the boundary EXPLICITLY NAMES it (`_boundary_names`). Presence of
`**kwargs` is not evidence of support: the first version forwarded on
permissiveness and `evals.run --suite daily-driver` went red on 26 arms with
`planner failed: ScriptedModel.__call__() got an unexpected keyword argument
'effort'`. That is the same trap `_boundary_streams` documents for `stream`,
and the fix is the same one. `runtime.model_router.call_model` names the
parameter, so production is unaffected and every older stub or scripted double
stays byte-identical.

The per-call record ALWAYS carries `effort` plus the router's
`effort_status` / `effort_sent` / `effort_parameter`, whether or not the
boundary could accept the keyword, and `cli/main.py::_result_json` publishes
them under one additive `effort` key - so `--json` can explain a cost claim,
and "asked for high" is never rendered as "ran at high".

### 2. `effort` is part of the resume identity

`effort_identity` joins `_IDENTITY_FIELDS` on the strict kernel path plus the
additive `shared.agent_contracts.Checkpoint.effort_identity` field, and
`harness/agent_kernel/checkpoints.with_effort` is the ONE seam that carries
the rung from a run's config into a `RunSpec`'s metadata - so the daily,
verified-fix and legacy strategies all agree. The rung is always non-empty
(`"auto"` at minimum), which means the existing "all identity fields must be
non-empty" rule in `checkpoint_identity_matches` also demands an effort field:
a checkpoint that never recorded a rung does not authorize a resume. A
mismatch takes the existing fail-closed path (fresh attempt, journalled).

### 3. Compaction defaults to the cheap tier

`DEFAULTS["context_compaction_tier"] = "cheap"` is a BEHAVIOUR-CHANGING
default and it is the fix the brief asked for. `DailyCodingStrategy._summary_gateway`
prefers the run's `model_tiers["easy"]` model (falling back to
`runtime.config.DEFAULT_MODEL_TIERS["easy"]`); `context_compaction_model`
still wins outright. The `context_compacted` receipt, the durable
`compactions.jsonl` row and the journal event all gain
`compaction_tier` / `_configured` / `_model` / `_honoured`, and the
pre-existing `compaction_model*` fields are unchanged in meaning - which is
why `tests/test_ceiling_r2_08_backoff_context.py` passes **25 passed**
untouched. A cheap tier that IS the run's own model returns the run's own
gateway, and an unrecognised tier falls back to `cheap` rather than to the
expensive run model, because a typo must not make summarisation cost frontier
prices.

`_absorb_spend` is unchanged: a cheap summariser's spend is still folded into
the run's own totals (already correct from R2-08).

### 4. Config keys and the one env-aware merge

`effort: "auto"` (behaviour-NEUTRAL - no parameter is sent, which is the only
reason it is safe in a table merged into every task and every eval arm),
`effort_parameter: None` (`None` means "use the family's knob", NOT "off"),
and `context_compaction_tier: "cheap"`. `harness.config.get_config` is the
ONE env-aware merge: `Task.config["effort"]` > `NEO_EFFORT` > `auto`,
checked against the CALLER's dict because `DEFAULTS["effort"] = "auto"` is
always present after the merge and would otherwise make the environment
unreachable. `EFFORT_ENV_VAR` is a literal in `config.py` with a comment
saying why (this module keeps zero runtime imports; the harness must stay
importable with no provider present).

### 5. Verification (this tree, `-p no:randomly`)

NEW `tests/test_agt_08_effort.py` -> **67 passed** (host-only).
`test_ceiling_r2_08_backoff_context.py` -> **25 passed**;
`test_context_budget_engine.py` -> **18 passed**;
`test_agent_kernel.py` + `test_agent_loop.py` -> **109 passed**;
`test_config_trace_state.py` + `test_model_router.py` +
`test_cli_command_system.py` -> **137 passed**;
`test_daily_driver_evals.py` + `test_evals_run.py` + `test_evals_tasks.py`
+ `test_modes.py` -> **168 passed**; `evals.run --check` -> **14/14 CLEAN**;
`evals.run --suite daily-driver --no-docker --json` -> **52/52 arms ok,
0 failures**, matching the recorded pre-round baseline.
Full evidence, the one host-load flake attributed, and the cross-terminal
requests are in `runtime/AGENTS.md` section AGT-08. **No Docker lane and no
live-provider lane were run** and neither is claimed.

## AGT-06 - compact the growth, not the prefix; replayable condensation; thrash
that fails loudly (2026-09-28)

**Files owned: `harness/agent_kernel/budget.py`,
`harness/agent_kernel/conversation.py`, two keys in `harness/config.py`, NEW
`tests/test_agt_06_context_compaction.py`. `runtime/prompt_cache.py` was READ
and never edited. Did NOT touch `harness/agent_kernel/strategy.py` (AGT-05's
alone), `harness/core.py`, `harness/agent_loop.py`, `runtime/checkpoint.py`,
`evals/**`, any verifier, or the mint condition.**

Three refinements to a budget that already measured tokens. All five required
proofs live in `tests/test_agt_06_context_compaction.py` (**19 passed**,
host-only: no Docker, no provider, no network).

### 1. Item 1 was ALREADY LANDED by AGT-04, and this round pins it at storage

`cap_tool_output` + `tool_output_token_limit` + the `_execute_calls` call site
are AGT-04's work, not this round's. What was missing was a proof that the cap is
applied where the text ENTERS the run. `test_a_runaway_tool_output_is_capped_before_anything_stores_it`
reads all four destinations - the conversation journal row, the trace
`tool_result` row, the next model request, and the `tool_output_capped` receipt -
and requires every one of them to be bounded.

**One composition finding, disclosed.** The storage cap keeps the HEAD and puts
its marker at the TAIL; `ConversationMemory.record_tool_result` then applies the
conversation's own character bound (`agent_conversation_tool_chars`), which also
keeps the head. So the `[neo-truncated` marker is usually cut and the
conversation's `...[compacted]` marker is what a reader actually sees. Both are
honest reports and the composition is correct, but **if you want the token cap's
own marker to survive into the journal, the character bound has to leave room
for it** - that is a change to `record_tool_result`'s `_bound` call, not to
`cap_tool_output`. Recorded rather than done.

### 2. Prefix-safe compaction - the head is not the budget's to spend

`prefix_identity(messages, tools=, breakpoint_index=)` returns
`PrefixIdentity(messages, sha256, tokens, source)`. It **reads
`runtime.prompt_cache.split_at_breakpoint` / `prefix_digest` as the ONE
authority** and falls back to the leading message plus a local digest, with
`source` saying which ran. `plan_drop` gained `protect_prefix=True` and unions
the frozen-prefix indexes into the guarded set **structurally**, so a caller
that forgets to pass `protect=` still cannot drop the prompt-cached head.

**Two boundaries, deliberately distinct - do not conflate them:**

| field | meaning | value in a seeded run |
|---|---|---|
| `cache_prefix_messages` | where `runtime.prompt_cache` would anchor a provider breakpoint | 1 (the system message) |
| `body_from` | the first index a compaction may touch | 2 (the base frame; 3 once a handoff exists) |

The handoff sits at `body_from` and grows with every compaction, which is exactly
why it is safe to replace: nothing in the provider's cache keys on it. The base
frame is what nothing else restates, so it is never replaceable.

`compact_tokens` measures `cache_prefix_identity()` before and after, and every
receipt carries `cache_prefix: {before, after, preserved}`. Through the real
kernel with a scripted model as the witness, **one digest for the whole run**
across nine turns and a real compaction. The `prefix_protected` property on
`DropPlan` is RECOMPUTED from `indexes` rather than asserted by the producer, so
a future selection change that reached the head would flip the receipt instead
of silently invalidating a cache every turn.

**The witness must filter on `step.startswith("agent-")`.** The compaction call
is a separate request with its own prompt; comparing its digest against the turn
requests' digest measures two unrelated prompts. That was a real false failure in
this round's first test draft, and it is the same class as the `model_delta`
defect UXP §6 recorded.

### 3. Replayable condensation - the OpenHands "what was forgotten" record

`CondensationRecord` (in `budget.py`) names every dropped message as a
`DroppedMessage` (seq, turn, kind, role, tool, ok, target, chars,
content_sha256) plus the replacement summary **verbatim**, the token counts, the
handoff as it stood BEFORE the condensation, both prefix digests, `body_from`,
and a `reversible` block naming the restore call.

**The dropped CONTENT is deliberately absent.** The durable journal is where it
lives; a record carrying the text would make the in-memory conversation grow
exactly as fast as the compaction meant to bound it. So reconstruction is a
two-part operation: `contents_by_seq(journal_rows)` +
`reconstruct_prior_view(record, base_messages=, live_messages=, contents=,
live_handoff=)`. `ConversationMemory.bind_contents(resolver)` binds a
`seq -> content` resolver; with none bound the answer is `None`, and one
unresolvable sequence fails the WHOLE reconstruction (a prior view with a hole in
it is not a prior view).

`live_handoff` is a required argument, not a convenience: the live view's handoff
message is the POST-compaction one, and without it a naive slice DUPLICATES the
handoff whenever the compaction was the first thing to create one. That was a
real bug in this round's first version, caught by the byte-exact
`rebuilt == before_view` assertion.

The record rides **three** places with no wiring change, because
`compact_tokens` returns a dict the strategy already fans out: the returned
receipt (so `compactions.jsonl` and the `context_compacted` event both carry it),
the journal's `compaction` row, and `memory.condensations`.

**The character/message fold is covered too** - a fold is a compaction by
another name, and if only the token path were inspectable the most frequent
compaction in a long run would be the one nobody can debug. It emits the same
`condensation` block on its journal row but is deliberately NOT appended to
`memory.condensations`: `_compact` runs on every append once the budget is hit,
and a per-append record would make the object grow as fast as the history.

### 4. Thrash fails loudly - and "progress" is the honest test

`CompactionThrashPolicy` / `CompactionThrashError` / `CompactionThrashError.as_dict()`.
`compact_tokens` raises on the third consecutive UNPRODUCTIVE compaction; the
condensation, its journal row, and a `compaction_thrash` journal row are all
written BEFORE the raise, so the evidence outlives the exception. The kernel
turns the strategy exception into `status="failed"` with the message as
`error` (`harness/agent_kernel/kernel.py:408`), so the abort is loud and can
never be a completion. A second call re-raises the SAME error: a guard whose
answer changes between calls is not a guard.

**The definition of progress took two attempts and the first was wrong.**
"`reclaimed_tokens > 0`" is not progress: a compaction can reclaim one token and
still have accomplished nothing, because its PURPOSE is to get the request back
under its trigger. The shipped rule is **`after_tokens < limit_tokens`**, with
the `min_reclaim_tokens` floor applying only to a caller that measures with no
trigger in the receipt.

**Blocked attempts count too, and that is the half that fires in practice.** A
request over its trigger whose every message is protected - the compiled base
frame alone being larger than the window is the real shape - cannot be
compacted at all. That used to be one `context_compaction_skipped` event per
turn, forever. It now feeds the same streak. **One blocked attempt is not an
abort** (it returns `None`, an ordinary skip) because one turn whose only
droppable message is the protected newest turn is ordinary, not a fault; three
unbroken ones are.

### 5. Reversibility is intact, and the snapshot grew two keys

`restore_compaction` is `runtime/checkpoint.py`'s and was NOT edited. The
`reversible` block on the record names it. `snapshot()`/`restore()` gained
`condensations` and `thrash`, so a restored run keeps the records it can be
asked to explain itself with; the existing byte-identical-render round trip is
pinned again.

### 6. Config - two keys, and why the guard is live anyway

`compaction_thrash_limit` (**3**, `0` disables) and
`compaction_min_reclaim_tokens` (**1**), both in `DEFAULTS`. The limit is a cap
and not a switch, for the same reason AGT-04's `max_repeat_tool_calls` is.

**`ConversationMemory` reads them through `configure_compaction(config)`, and
the strategy does not call it yet** - see the request below. Until it does, the
constructor default (which equals the `DEFAULTS` value) applies, so the guard is
live at 3 either way. A value in `DEFAULTS` merges into every task, and a
default that only exists when asked for is not a default.

### 7. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_agt_06_context_compaction.py` -> 19 passed.** All five
  required proofs are present and named:
  `test_a_runaway_tool_output_is_capped_before_anything_stores_it`,
  `test_the_cached_prefix_is_byte_identical_across_a_compaction` **and**
  `test_the_kernel_keeps_the_cached_prefix_across_a_real_compaction`,
  `test_a_dropped_message_record_reconstructs_the_prior_view`,
  `test_a_thrashing_compaction_aborts_with_a_reason` **and**
  `test_a_thrashing_run_ends_failed_and_never_completed`, and
  `test_restore_compaction_still_undoes_one_condensation`. Plus the structural
  prefix proof, the honest-`None` reconstruction control, the fold path, the
  snapshot round trip, the never-compact-again proof, the
  healthy-run control, the policy/off-arm matrix, and the verifier-gate pin.
- `tests/test_agt_06 test_context_budget_engine test_agent_kernel
  test_agt_04_search_budgets test_agt_03_edit_lint test_prompt_cache_cost
  test_config_trace_state` -> **209 passed, 1 skipped** (a Windows
  symlink-privilege case).
- `tests/test_agent_loop test_recovery_steering test_cli_runview test_slos
  test_tracing test_modes test_ceiling_r2_08_backoff_context` -> **365 passed**.
- `tests/test_daily_driver_evals test_evals_run test_evals_tasks
  test_ceiling_r2_08_backoff_context test_ceiling05_knowledge
  test_tool_protocol` -> **148 passed, 2 failed**. **Both failures are
  attributed, NOT counted as passes, and NOT weakened:**
  `test_daily_driver_evals.py::test_quick_matrix_runs_real_comparison_with_receipts`
  and `::test_feature_evidence_lane_covers_every_active_feature`. Both turn on
  `core_run_succeeded: result.status == "success"` at
  `evals/daily_driver.py:3864` and ten other sites - a probe predicate on the
  legacy `harness.core.run_task` path, which reports the honest
  `completed_verified`. The same defect CEILING-05 recorded. Proof it is not
  this round: importing `harness.core` in a fresh interpreter loads **no**
  `harness.agent_kernel.*` module at all (measured), and the first failing
  feature's run is `status: completed_verified` with only the probe predicate
  false. `tests/test_daily_driver_evals.py` was not edited.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0 (re-run after the
  final edit).
- `python -m evals.run --suite daily-driver --no-docker --json` -> **39/52 arms
  ok, 19 valid comparisons, `zero_false_verified_successes=true`,
  `zero_lost_edits=true`, `zero_unauthorized_mutations=true`, $0.0246**,
  readiness `NOT_READY`. **This is RED against AGT-04's recorded 52/52, and it
  is reported as red.** Attribution, with the evidence: **zero** `compaction_thrash`
  rows in any artifact journal and zero `"compaction made no progress"` strings,
  so the new guard never fired anywhere in the matrix; the feature lane fails
  only on the `== "success"` predicate above; `dd_06`/`dd_09` crash with
  `IndexError` INSIDE `evals/daily_driver.py:7025` and `dd_08` with
  `FileNotFoundError` - probe-side, in a file this round did not touch; and
  `dd_07_context_continuity`, `dd_10_mutation_approval` and
  `dd_06_connector_failure` all **pass standalone** with every assertion true.
  `dd_05_skill_model_context` fails standalone too, deterministically, on
  `fix_verified` - the same legacy probe predicate. No assertion was weakened
  and no probe was edited to make this green.
- `ruff check` clean on all four touched/added files; `ruff format` applied to
  the new test only (the three harness files are shared dirty files with
  parallel in-flight edits); `python -m compileall -q harness` clean.
- **No Docker lane and no live-provider lane were run.** Every model call in
  this suite is a scripted double.

### 8. Not implemented / honest notes

- **The thrash keys are not wired from the run config into the memory.** One
  line, filed below. The guard runs at the shipped default.
- **`ConversationMemory.bind_contents` is not called by the kernel**, so
  `memory.reconstruct_prior_view(...)` returns `None` on a live run until it is.
  The module-level `reconstruct_prior_view` is the working surface and is fully
  tested. The journal is the intended resolver.
- **The fold path's `cache_prefix_sha256_after` is set to the before value.** The
  fold pops from `_history` only, so the base frame cannot change, but it is a
  derived value rather than a second measurement. The token path measures both.
- **`reconstruct_prior_view` needs the live handoff state**, so it is a
  four-argument call rather than a one-liner over the record alone. The
  alternative - sniffing the rendered handoff's heading out of the message list -
  was rejected: a content marker is a weaker contract than a value.
- **The thrash guard aborts the whole run.** A softer policy (warn N times, then
  keep the base frame and drop the constraint re-injection) would be more
  forgiving and is a real option for a future round; this one follows the brief's
  "bail with an error".
- **`prefix_identity` without `tools` digests the message head only.** The
  strategy does not pass the request's schemas into `compact_tokens`, so the
  receipt's digest is over messages. Tool schemas are constant within a run, so
  this cannot hide a per-run prefix change; a caller that wants the provider's
  exact digest passes `tools` (the test does).
- **No cost measurement.** A prefix-stable compaction should reduce cache-write
  spend, and `runtime.prompt_cache`'s ledger would show it. No provider lane was
  run, so no saving is claimed.

### 9. Cross-terminal requests

1. **`harness/agent_kernel/strategy.py` (AGT-05's) - ONE line each, both
   additive.** In the `ConversationMemory(...)` constructor call at
   `strategy.py:258`, nothing is required for correctness; these make the
   configured values take effect:
   ```python
   self.conversation = ConversationMemory(
       max_messages=int(cfg.get("agent_conversation_messages", 48)),
       max_chars=int(cfg.get("agent_conversation_chars", 24000)),
       max_tool_output_chars=int(cfg.get("agent_conversation_tool_chars", 4000)),
       handoff_max_chars=int(cfg.get("agent_conversation_handoff_chars", 4000)),
       journal=self.conversation_journal.append,
       # AGT-06: the thrash keys, plus the content resolver that makes
       # `memory.reconstruct_prior_view` work on a live run.
       compaction_thrash_limit=int(
           cfg.get("compaction_thrash_limit", DEFAULT_COMPACTION_THRASH_LIMIT)
       ),
       compaction_min_reclaim_tokens=int(
           cfg.get("compaction_min_reclaim_tokens", DEFAULT_MIN_RECLAIM_TOKENS)
       ),
       contents=self.conversation_journal.content_for_seq,
   )
   ```
   `runtime/checkpoint.py` would need a `ConversationJournal.content_for_seq(seq)`
   reader (Terminal 3's file, not edited here); until it exists, pass
   `contents=lambda seq: <lookup over journal rows>` or omit it. Both
   `test_a_thrashing_run_ends_failed_and_never_completed` and
   `test_the_thrash_guard_reaches_the_live_conversation_memory` are written to
   tell you the day this lands.
2. **`harness/agent_kernel/strategy.py` - A REAL BUG, found by this round, not
   fixed here.** `_compile_knowledge` (`strategy.py:2272`) does
   `knowledge = self.knowledge(spec); if knowledge is None: return ""`, but
   `knowledge()` sets `self._knowledge = False` for the documented
   `knowledge_enabled=False` OFF arm (`strategy.py:2225-2226`) and for an
   unimportable knowledge module. `False is not None`, so the next line is
   `False.compile()` and **the run dies with `'bool' object has no attribute
   'compile'`**. `knowledge_enabled=False` is the single documented OFF arm, so
   it currently crashes the daily path. The fix is `if not knowledge: return ""`
   (or make `knowledge()` return `None` instead of `False`). Reproduced while
   writing `test_a_thrashing_run_ends_failed_and_never_completed`; I removed the
   key from that test rather than work around it in someone else's file.
3. **`evals/daily_driver.py` (Terminal 10's) - the probe predicate, ten sites.**
   `"core_run_succeeded": result.status == "success"` cannot be satisfied by an
   honest run: `harness.core.run_task` reports `completed_verified`, and
   `harness/AGENTS.md` records that mapping `completed_unverified` -> `success`
   as a violation the project forbids. Line 4072 in the same file already uses
   the correct `outcome.status == "completed_verified"`. This is the sole cause
   of both `test_daily_driver_evals.py` failures and of the 13 red matrix arms
   above; `dd_06`/`dd_09` additionally crash with `IndexError` at
   `daily_driver.py:7025` on a missing model reply. Not my file, not edited.
4. **`harness/agent_loop.py`'s legacy `BASH` path has none of this.** The
   per-tool storage cap, the prefix safety, the condensation record and the
   thrash guard are all on the typed-kernel path. AGT-02 owns that file.
5. **`INTERFACES.md` owner:** AGT-06 added no Boundary 0-5 signature change. New
   public surface, all in `harness/agent_kernel/budget.py`:
   `PrefixIdentity`, `prefix_identity`, `DroppedMessage`, `CondensationRecord`,
   `CompactionThrashPolicy`, `CompactionThrashError`,
   `compaction_thrash_policy_from_config`, `DEFAULT_COMPACTION_THRASH_LIMIT`,
   `DEFAULT_MIN_RECLAIM_TOKENS`, `THRASH_REASONS`; plus
   `plan_drop(..., protect_prefix=, tools=, breakpoint_index=)` and
   `DropPlan.{prefix_messages, prefix_sha256, body_from, prefix_protected}`. In
   `harness/agent_kernel/conversation.py`:
   `ConversationMemory.{base_messages, cache_prefix, cache_prefix_identity,
   condensation, reconstruct_prior_view, bind_contents, configure_compaction,
   thrash_policy, no_progress_streak, condensations, thrash_error}` and the
   module functions `reconstruct_prior_view`, `contents_by_seq`. `snapshot()` and
   `restore()` gained `condensations` and `thrash` (both default-tolerant).
   The compaction receipt dict gained `cache_prefix` and `condensation`. A
   Change Log entry belongs here; I did not edit `INTERFACES.md`.
6. **`shared/traceview.py` / `cli/runview.py`:** the new `compaction_thrash`
   journal row and the `cache_prefix` / `condensation` blocks on
   `context_compacted` are additive and unclassified. A live TUI run will render
   the event as an unmapped kind - the same `EVENT_VOCABULARY` gap AGT-02
   filed for `reflection`. `context_compacted` already renders, so the receipt is
   visible there; only the abort row needs a vocabulary entry.

## AGT-07 - two axes, and an approver that fails closed (2026-09-28)

**Files owned: NEW `harness/approver.py`, the AGT-07 region of
`harness/agent_kernel/policy.py`, six `None` keys in `harness/config.py`, NEW
`tests/test_agt_07_two_axis_approver.py`. Did NOT touch `harness/core.py`,
`harness/agent_loop.py`, `harness/agent_kernel/strategy.py`, any verifier, or
the mint condition (`target_test_passed and regression_passed and not flaky` is
byte-identical).**

The finding behind the round: "it did not ask" and "it could do anything" were
one sentence. R2-15 made the prompting axis honest; nothing described the other
one, so a reader of any receipt had to guess.

### 1. Axis (b) now reports BESIDE axis (a), and cannot be built without it

`harness/agent_kernel/policy.py` gained `ContainmentAxis`, `SafetyReceipt`,
`PolicyEngine.evaluate_axes(call, *, containment=None, containment_known=True,
context=None)` and `.privileged_receipt(...)`. Both delegate to `evaluate`, so
the policy's behaviour is unchanged; they wrap its decision.

The separation is mechanical, which is the only kind that survives a refactor:

* `SafetyReceipt.from_decision_only(decision)` is the ONLY constructor that
  omits a containment receipt and it yields `ContainmentAxis.unknown()` - a
  caller holding a decision cannot obtain a containment claim by forgetting.
* `ContainmentAxis.contained` is False while the axis is unknown, so "we do
  not know" can never render as "it was fine".
* `requires_human` is True exactly for an `ask` (an `allow` from a covering
  grant is not a pending escalation; a `deny` is not a question for anyone),
  and it is derived - no constructor argument can set it.
* `SafetyReceipt.axes_confused()` reports a receipt whose containment half
  carries a prompting field, or whose decision half carries a sandbox field, or
  whose unknown axis claims to be sandboxed. `summary()` then renders
  `[AXES CONFLATED: ...]`. A future edit that merges the axes fails a test
  instead of shipping a lie.
* A hard deny stays a deny whatever the containment is: containment is not a
  mitigation channel.

`approver_prompt_context()` is the ONLY containment surface the approver model
sees, and it is informational: an approver's approval never widens
containment, and containment never decides whether to prompt.

### 2. NEW `harness/approver.py` - the four properties, in check order

`review()` runs admissibility -> enable switch -> bounded call -> fail-closed
parse, and **nothing after the parse can turn a denial into an approval**.

* **Only already-privileged actions.** `shared.approval.approver_admissible`
  is consulted BEFORE a prompt is built, so an unprivileged action never
  reaches a model at all (`not_privileged`, and `recorder.calls == 0` is a
  test). A cheap model must not decide what a human is asked about.
* **Fail closed, with no opt-out.** Unparseable / ambiguous / malformed /
  reason-less replies, a TIMEOUT, a provider exception, a disabled agent and
  an untraceable subagent escalation are all denials, each with a distinct
  `failure` from the closed `APPROVER_FAILURES` set. `ApproverReply.approved`
  is True only for a parsed, reasoned approval, so a caller that ignores
  `failure` still denies. The wait is bounded on a daemon thread and the
  DECISION does not wait for a slow model (measured: a 2 s reviewer at a 0.25 s
  budget returns a denial in < 1.75 s).
* **Origin is named everywhere.** Prompt, verdict and journal row all carry
  `ApprovalOrigin`; `describe()` prints `subagent sub-7 spawned by planner,
  escalating to main thread thread-main`.
* **Journalled and redacted.** `logs/{task_id}/approvals/<task>-approver.jsonl`,
  one redacted JSONL row per decision with the reasoning - a SEPARATE file
  from `trace.jsonl` for the same reason the trust receipt is (the trace has
  contiguous sequence allocation and a second writer corrupts
  `replay_run`).

"Cheap" is expressed as `difficulty_hint="easy"` on the Boundary-2 boundary
rather than a hardcoded model name, so it is cheap on every provider. The
boundary is called with only the keyword arguments its own signature declares,
so a `messages -> str` double, a keyword router, and the real router all work
unchanged.

### 3. The reply grammar is NOT here

`shared.approval.parse_approver_reply` owns what an approval MEANS (a JSON or
first-word-text decision PLUS a non-empty reason; everything else denies), so
this module, a TUI approver and a CLI approver cannot drift on the vocabulary.
See `shared/AGENTS.md` for the failure table.

### 4. Config: six `None` keys, deliberately not values

`approver_agent` (master switch, key presence + truthiness),
`approver_model`, `approver_timeout_s`, `approver_max_chars`,
`sandbox_readonly_paths`, `sandbox_writable_roots`. `None` is what an absent
key already meant to every approver consumer, so publishing them is
behaviour-neutral; the bounded numbers live in `harness/approver.py`
(`DEFAULT_TIMEOUT_S = 20.0`, `DEFAULT_MAX_CHARS = 4000`). A value in `DEFAULTS`
merges into every task and every eval arm, so an approver model call on every
privileged action is an operator's decision, never a default.

### 5. Verification (this tree, `-p no:randomly`, real Docker 28.5.1)

- NEW `tests/test_agt_07_two_axis_approver.py` -> **37 passed** (34 host-only
  + 3 real-container). All six required proofs are present and named:
  `test_an_unparseable_approver_reply_denies`, `test_a_timeout_denies`,
  `test_the_approver_only_sees_already_privileged_actions`,
  `test_network_is_off_unless_declared`, `test_dot_git_is_read_only_inside_a_
  writable_root`, `test_a_subagents_escalation_is_labelled_with_its_origin`.
  The NON-VACUITY controls matter more than the proofs here, because an
  approver that denies everything passes the first three:
  `test_a_well_formed_approval_is_an_approval` (and the real-Docker
  `readonly_paths=[]` control), plus a whole class for the two-axis claim and
  one that asserts no completion vocabulary exists anywhere in
  `harness/approver.py`.
- `tests/test_agent_kernel.py tests/test_tool_protocol.py tests/test_modes.py`
  -> **175 passed**; `tests/test_ceiling_r2_17_daily_truth.py
  tests/test_agent_loop.py tests/test_recovery_steering.py` -> **197 passed**;
  `tests/test_ceiling_r2_15_trust.py tests/test_difficulty_approval.py
  tests/test_config_trace_state.py` -> **79 passed**;
  `tests/test_sandbox.py tests/test_ceiling_security.py
  tests/test_workspace_security.py` -> **154 passed**;
  `tests/test_verify.py tests/test_verify_js.py` -> **68 passed** (real
  Docker); `tests/test_e2e_run_task.py` -> **28 passed** (real Docker);
  `python -m evals.run --check` -> **14/14 CLEAN**. `ruff check` clean on all
  five owned files; `ruff format` applied to the two NEW files only.
- **No live-provider lane was run and none is claimed** - every approver reply
  in the suite is a scripted string, so nothing here is evidence about how any
  real provider answers.

### 6. NOT wired - a filed request, not a pass

`harness/approver.py` has **no production call site**. The approval prompts in
`cli/interactive.py` (`_trusted_approver`, ~:7389), `cli/tui.py`,
`harness/agent_loop.py` (`_request_approval`, ~:1639) and
`runtime/approval.py` are unchanged, and each of those files is another
terminal's. The shape a surface should use:

```python
receipt = policy.privileged_receipt(call, containment=containment_receipt)  # axis (a)+(b)
if receipt.requires_human:                       # already needed a human
    verdict = harness.approver.ApproverAgent.from_config(
        cfg, call_fn=..., journal_path=journal_path_for(log_dir, task_id),
    ).review(
        canonical_effect("shell", argv, working_directory=..., environment=...),
        origin=ApprovalOrigin(thread_id=run_id, subagent_id=..., parent_id=...),
        requires_human=receipt.requires_human,
        privileged_reason=receipt.decision.reason,
        containment=receipt.approver_prompt_context(),
    )
    if not verdict.approved:      # fail-closed; verdict.summary() is the overlay line
        ...
```

A CLI approver (the human's own `y/n`) must be consulted for the SAME request
the reviewer saw; if the two disagree, the human wins, and the reviewer never
becomes the thing that satisfies the human's slot.

### 7. Red results, both attributed, neither weakened

- **`evals.run --suite daily-driver --no-docker` -> 49/52 arms ok** (the
  AGT-04 recorded baseline was 52/52), with
  `zero_false_verified_successes=true`, `zero_unauthorized_mutations=true`,
  `zero_lost_edits=true` - the three honesty guards unchanged.
  `dd_05_skill_model_context` fails both arms on `fix_verified`, and
  `dd_20_live_tui_status_diff/baseline` is the already-recorded
  host-load flake. **`dd_05` is AGT-08's, proven two ways:** (a) a controlled
  A/B with a `sitecustomize.py` that forces the PRE-AGT-07 argv
  (`readonly_overlays = ()`, `writable_overlays = ()`) gives the
  **byte-identical** failed result; (b) the run's own
  `logs/skill-content/trace.jsonl` says
  `task_end reason="planner failed: ScriptedModel.__call__() got an unexpected
  keyword argument 'effort'"` - AGT-08's effort ladder is landing in this
  shared tree and the eval's scripted model does not accept `effort` yet.
  Neither is this round's, and no assertion was relaxed to hide either.
- One combined pytest run (`test_sandbox + test_ceiling_security +
  test_workspace_security`) reported `1 failed, 53 passed` and then died with
  `MemoryError` inside pytest's own failure repr; the same three files passed
  154/154 on re-run and each passed standalone. Host pressure under four
  parallel terminals, recorded rather than excused.

## AGT-03 - the lint check runs INSIDE the edit, before the write (2026-09-28)

**Files owned: `harness/lint.py`, `harness/editor.py` (validation region +
`EditOutcome`), `harness/tools.py`, plus NEW `tests/test_agt_03_edit_lint.py`.
Did NOT touch `harness/core.py`, `harness/agent_loop.py`, `harness/codemod.py`,
`harness/config.py`, `execution/**`, or any verifier. The mint condition
(`target_test_passed and regression_passed and not flaky`) is untouched: this
gate refuses MUTATIONS and has no say in whether a run completed.**

SWE-agent measured **-15.0%** without an in-edit guardrail. The reason is
structural, not a threshold: the loop's lint gate runs *after* an edit, so a
broken edit exists in the work tree and is then rolled back. A model that reads
the tree on the next turn reads the harness's own damage.

### 1. The one insight that makes it cheap

`apply_text_edit` builds the post-image as a **byte splice**
(`before[:offset] + new_bytes + before[offset+len(old_bytes):]`). At the moment
the old code called `_atomic_write_bytes`, the exact post-image bytes were
already in memory. So the check does not need a file, a temp file, or a second
pass over the disk: it is a pure function of a string.

```python
harness.editor.precommit_check(candidate_src, relative_path, *, config=None) -> harness.lint.EditCheck
```

`apply_text_edit` calls it on the candidate, and on `failed` returns before the
write. The file is byte-identical to its pre-edit state because **nothing was
ever written** - there is no rollback to perform, only a candidate to discard.

### 2. The two things that are easy to get wrong, and are pinned

**A refusal is not a pass.** The status is a closed tri-state
(`harness.lint.CHECK_STATUSES` = `passed | failed | unchecked | disabled`),
and `harness/tools.py` adds a fifth (`not_applicable`) for a mutation with no
candidate content. `EditCheck.checked` is True only for `passed`/`failed`. An
`.md` edit, a `.txt` edit, a JS file on a host without the tree-sitter grammar,
and a non-UTF-8 file are all `unchecked`/`not_applicable` WITH a reason, and a
successful edit whose file had no checker says `[in-edit lint: unchecked - ...]`
**in its own tool result**. `_ts_syntax_status` exists purely so "the grammar is
not installed" is a value the gate can report; the loop gate still collapses it
to a silent pass, which is right for its cost model and wrong for this one.

**The refusal has to be actionable.** `±3` lines of context
(`harness.lint.DEFAULT_CONTEXT_LINES`), real line numbers, the offending line
marked with `>`, per-line truncation at 200 chars, and a whole-block cap of 1200
chars with an explicit `[... context truncated ...]`. "syntax error at line 2"
is not a form a model can self-correct from.

### 3. What is covered, and what is honestly NOT

| mutating tool | screened? | how |
|---|---|---|
| `edit` (catalog + legacy) | **yes** | `apply_text_edit` checks the splice pre-commit |
| `write` | yes, in the facade | candidate content is the argument; no disk read |
| `apply_patch` | yes, in the facade | hunks are reconstructed and applied per file |
| `rename` | yes, in the facade | same bytes, screened under the DESTINATION's extension |
| `rename_symbol` / `update_signature` | yes, in the facade | every replacement is an exact old/new pair |
| `delete` / `undo` | n/a | no candidate content; the loop gate is the audit |
| **`SafeToolBackend.execute` (the typed kernel's real dispatcher)** | **NO** | see request 1 |
| **`harness/codemod.py::apply_plan` (dispatches through that backend)** | **NO** | see request 1 |
| **`harness/agent_loop.py`'s EDIT/WRITE verbs** | **NO** | see request 2 |

The three un-covered paths all keep their own post-write protection, so nothing
broken lands anywhere - this is a coverage gap, not a defect. Two self-arming
pins in the new suite FAIL the moment either file is wired, so the gap cannot be
forgotten:
`test_the_codemod_path_does_not_yet_use_the_in_edit_gate`,
`test_the_workspace_backend_owner_has_a_one_call_hook_to_wire`.

### 4. Receipt semantics worth reading before you touch `rolled_back`

`rolled_back` keeps its documented meaning: **the file on disk is
byte-identical to its pre-edit state**. A pre-commit refusal sets it `True`
because that is literally true, and `pre_commit=True` +
`write_mode="pre_commit_refused"` say which mechanism got there. That is why
R2-06's `test_a_failed_syntax_check_rolls_the_file_back_byte_for_byte` still
passes unchanged. Do NOT "fix" this by making `rolled_back` False - that field
is what a caller reads to know the tree is clean, and it would be False while
the tree is in fact untouched.

The two-call rollback path still exists and is still correct, for a
caller-supplied `validate`, whose `(root, [paths])` signature reads the tree and
so can only run after the write. That is the only remaining use of the rollback
code, and R2-06's atomic-write test was extended to pin BOTH mechanisms.

### 5. Config - three keys, deliberately NOT in `DEFAULTS`

| key | default | why |
|---|---|---|
| `edit_inline_lint` | on | only an explicit `False` disables; absent and `None` both mean on |
| `edit_inline_lint_names` | **off** | the undefined-name pass is false-positive-prone; a false positive here refuses a mutation outright, while the loop gate runs the same pass moments later for the cost of one wasted attempt |
| `edit_inline_lint_context_lines` | 3 | the brief's ±3 |

A value in `harness/config.py::DEFAULTS` is merged into every task and every eval
arm, so publishing one would silently switch every run. The R2-03/R2-07
precedent applies: bounded internal defaults here, and
`test_no_in_edit_lint_key_is_in_the_harness_defaults` fails if one is published.
A `None`-valued discoverability entry in `DEFAULTS` would be behaviour-neutral
and is the recommended change if the owner wants the keys visible.

### 6. One incidental fix, disclosed

`harness.lint.check_syntax` used to `compile()` **every** non-JS/TS file, so a
changed `notes.txt` containing prose produced a bogus `syntax error` finding
from the LOOP gate (`compile("hello world", "notes.txt")` raises). Verified
against `git show HEAD:harness/lint.py`. Routing the suffix->language decision
through one `LANGUAGE_BY_SUFFIX` table fixes it: an unrecognised extension is
now ignored, which can only ever REMOVE a false refusal, never a real finding.
`tests/test_agt_03_edit_lint.py::test_the_loop_gate_still_ignores_files_it_
cannot_check` pins the new behaviour and
`::test_the_loop_gate_still_fires_for_an_edit_that_slipped_through` pins that the
gate still fires. The 2 pre-existing ruff findings in that file (RUF022, B905)
are the ratchet's and were left alone.

### 7. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_agt_03_edit_lint.py` -> 37 passed.** Host-only: no Docker,
  no model, no network. All five required proofs are present and named:
  `test_a_syntax_breaking_edit_is_refused_and_the_file_is_byte_identical`,
  `test_the_refusal_carries_three_lines_of_context_either_side` (asserts the
  exact line numbers `[7,8,9,10,11,12,13]` and the `>` marker),
  `test_a_valid_edit_is_applied_and_reports_the_check_as_passed`,
  `test_a_file_with_no_checker_is_reported_as_unchecked_not_passed`, and
  `test_the_loop_gate_still_fires_for_an_edit_that_slipped_through`. Plus the
  catalog coverage matrix, the multi-file-patch fold, the real
  `SafeToolBackend` root resolution, the strict-UTF-8 rule, the two context
  caps, the `None`-means-on switch, and the three honesty pins.
- `tests/test_ceiling_r2_06_editing.py` -> **34 passed.** One test was changed
  and it was made STRONGER, not weaker - see section 4.
- `tests/test_ceiling_r2_07_codemod.py` -> **83 passed**;
  `test_agent_kernel` + `test_config_trace_state` -> **144 passed**;
  `test_agent_loop` -> **62 passed**; `test_batch_docs_lint` +
  `test_multilang_graph` -> **66 passed**; `test_tool_protocol` +
  `test_editor_prompts` + `test_adversarial` + `test_workspace_security` ->
  **148 passed, 1 skipped**; `test_ceiling_r2_03_config_guard` +
  `test_ceiling_r2_12_polyglot` + `test_coordination` -> **188 passed, 1
  skipped**; `test_evals_run` + `test_evals_tasks` +
  `test_daily_driver_evals` -> **69 passed**. Every skip is a Windows
  symlink-privilege or platform case, not a Docker or provider pass.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0.
- `python -m evals.run --suite daily-driver --no-docker --json` ->
  **52/52 case arms, 26/26 valid comparisons, 28/28 feature-evidence arms,
  0 failures, `zero_false_verified_successes=true`, `zero_lost_edits=true`,
  `zero_unauthorized_mutations=true`, cost $0.0246** - byte-identical to the
  AGT-02 / AGT-04 recorded baseline, i.e. this round changed no arm's
  behaviour. Readiness is reported **`NOT_READY`** and that is honest: the
  Docker lane, the live-provider lane and the sampled manual-repair evidence
  were not selected.
- `ruff check` is within the ratchet's existing allowance: the only 2 findings
  in the three owned files are the pre-existing RUF022 / B905 in
  `harness/lint.py`, both recorded in `scripts/lint-baseline.txt` and both
  verified to be present on `git show HEAD:harness/lint.py` too.
- **No Docker lane and no live-provider lane were run**, and neither is
  claimed. No credential was inspected, requested, or retained.

### 8. Cross-terminal requests

1. **`execution/workspace.py::SafeToolBackend.execute` - ONE call, and it also
   covers the codemod path.** `apply_plan` in `harness/codemod.py` dispatches
   every replacement as `backend.execute("edit", {...})`, so wiring the backend
   covers both at once. Insert right after the schema/authorization block,
   before any mutation branch runs:
   ```python
   gate = harness.tools.precommit_check_call(
       harness.tools.backend_repo_root(self), canonical, values,
       config=self._task_config,
   )
   if gate.get("refused"):
       return ToolResult(
           False, canonical, policy.effect, policy.backend,
           value=dict(gate),
           error=(
               f"the mutation to {gate['path']} was refused before it was "
               f"written: {gate['message']}. NOTHING was written."
           ),
           policy=policy_value, profile=profile.value,
       )
   ```
   `harness.tools.backend_repo_root(self)` is there so nobody has to
   re-derive that `SafeToolBackend` exposes the root as
   `self.workspace.root`. `precommit_check_call` never raises and always
   returns a `status`, so a non-mutating call costs one dict and no I/O. Do
   NOT call `harness.editor.apply_text_edit` from here: the backend's
   revision / journal / undo semantics are its own, and the gate is
   deliberately a pre-flight rather than a second writer.

2. **`harness/agent_loop.py` - the legacy EDIT/WRITE verbs.** The general agent's
   legacy loop does its own replace-and-write and reads none of this. Either
   route it through `apply_text_edit` (R2-06 request 1, still open) or call
   `harness.tools.precommit_check_call` on the post-image it is about to write.
   Same one-call shape as request 1; `require_edit_digest` is not the issue
   there, only the missing check.

3. **`harness/codemod.py` - only if the owner prefers plan-level checking.**
   The backend wiring in request 1 already covers it at dispatch time. A
   plan-level alternative is to check each file's post-image after splicing
   every planned span and refuse the plan before dispatching any of them -
   strictly better (all-or-nothing already exists there) but redundant once (1)
   lands. Do not do both.

4. **`harness/config.py` (R2-04's file) - optional discoverability.** Three
   `None`-valued entries (`edit_inline_lint`, `edit_inline_lint_names`,
   `edit_inline_lint_context_lines`) are behaviour-neutral, because an absent key
   already takes the module's internal default. The pin
   `test_no_in_edit_lint_key_is_in_the_harness_defaults` currently rejects ANY
   `edit_inline_lint*` key; update it in the same change if you add them.

5. **The loop gate stays.** `harness/core.py` was not touched.
   `test_the_loop_gates_lint_module_is_still_wired_into_the_run_loop` reads
   `core.py` and fails if `lint_changed`, the `lint_gate` config read, or the
   `lint_failed` event ever go away. If someone proposes deleting the loop gate
   because "the in-edit check covers it", that test is the answer: the in-edit
   check sees ONE call's arguments; the loop gate sees every changed file,
   including a shell redirect's.

## AGT-04 - search budgets, concurrency classes, doom loop (2026-09-28)

**Files owned: `harness/retrieval.py`, `harness/tools.py`,
`harness/agent_kernel/strategy.py`, `harness/agent_kernel/budget.py`,
`harness/agent_kernel/tools.py`, plus eight defaulted keys in
`harness/config.py`. Did NOT touch `harness/core.py`, `harness/agent_loop.py`,
`harness/completion.py`, any verifier, or the mint condition.**

Three resource problems, one round: search could spend the whole context on one
grep; every tool call was serialised unless the WHOLE batch was read-only; and
an identical repeated call was a silent refusal the model simply re-emitted.
All five proofs live in `tests/test_agt_04_search_budgets.py` (42 passed,
1 skipped, host-only - no Docker, no provider, no network).

### 1. The search cap ERRORS, and there is no page two

`harness.retrieval.search_repo` is the ONE bounded search. Above
`search_max_matches` (50) it returns `ok=False, error="too_many_matches"` with
the count, the file count, and at most 10 sample lines - never the match set.
The count is a LOWER BOUND and says so (`total_is_lower_bound`), because the
scan stops at cap+1, which is all it needs to know. `files_not_searched_known`
separates "the walk finished" from "the walk hit its bound and the remainder was
never measured" - reading an absent count as 0 is the exact trap the module's
existing receipts exist to prevent.

**The paging affordance is a closed set, not an omission.**
`PAGING_ARGUMENTS` (28 names) is the single definition, used by
`search_repo` (which refuses one with `paging_refused`), by
`paging_affordances()` (which audits the live 45-tool catalog), and by the
tests. `start_line`/`end_line` are deliberately EXCLUDED - they bound a line
window of one file (`git_blame`), which is a narrowing, not a page turn.
`max_results` narrows a successful set but is clamped to the cap, so a caller
cannot buy a pass past the threshold.

**The kernel's `grep` and `glob` handlers now route through `search_repo`**
instead of hand-rolled `rglob` scans. `glob` previously returned
`sorted(hits)[:200]` - a silent truncation, which is the thing AGT-04 removes.

### 2. Concurrency is DECLARED on the catalog entry, not a hardcoded list

`harness.tools.ToolSpec` gains `read_only: bool | None`. `None` derives from
`side_effect_class == "read_only"`; all 16 read-only entries declare it
explicitly. It is part of `_catalog_shape`, so it is inside
`catalog_fingerprint()` AND `catalog_parity_report()` - **the fingerprint value
changed**, so any external consumer pinning it must be re-derived (nothing
in-tree does).

`ToolRegistry` gained `is_concurrent(call) -> bool` (branch on this),
`concurrency_class(call) -> "concurrent"|"sequential"` (reporting),
`note_repeat`, `preview_repeat`, `arm_loop_guard`, `loop_guard_report`.

**Two real bugs this round's own tests caught, both now fixed:**

1. `bool("sequential")` is `True`. The dispatcher passed
   `concurrency_class` (a STRING) as `plan_fanout`'s predicate, so **every
   mutation was scheduled as concurrent** - four sequential writes ran in
   0.17 s instead of 0.62 s. Fixed on both ends: `plan_fanout` reads its answer
   STRICTLY (only `True` or the literal `"concurrent"`), and the dispatcher uses
   the boolean `is_concurrent`. If you add a scheduling predicate, this is the
   trap you will hit.
2. `plan_fanout` originally bounded only the read-only class, so a turn of 200
   writes was unbounded. `max_tool_fanout` now bounds the TOTAL, spent
   read-only first (a read is how a model finds out what to mutate), and an
   over-budget call is REFUSED with a reason - never deferred, because a
   mutation that quietly ran a turn late is a lost edit.

### 3. A doom loop is a DECISION for the user, and it is counted once

The pre-flight runs before any dispatch and returns
`CompletionPolicy.needs_input` - the run STOPS and a human decides. Silently
refusing (`loop_detected`) does not stop a run: the model re-emits and the loop
continues to burn turns.

**The counting is the subtle part, and it was wrong first.** The pre-flight
originally used a pure `preview()` and recorded nothing, so the count never
advanced past 1 and the detector could never fire. Worse, once recording was
added, the registry's own guard would have counted the SAME call again and
reached the bound at half the turns the receipt names. The resolution:
`note_repeat` records and returns `(blocked, count, fingerprint)`; the strategy
passes those fingerprints in the dispatch context as `loop_guard_pre_checked`,
and `ToolRegistry._check_loop` skips them. One observation per call. The
registry's refusal stays for callers that dispatch directly (its own
`dispatch`, an SDK, a test) - a guard whose only enforcement point is the loop
above it is not a guard.

`max_repeat_tool_calls` default moved 2 -> 3. Read-only tools stay exempt
(`loop_guard_read_only: False`): re-reading a file is legitimate exploration,
and this repo already paid for a version that refused it.

### 4. Tool output is capped at STORAGE time

`cap_tool_output` (in `budget.py`) caps each result in TOKENS
(`tool_output_token_limit`, 4000; `0` = no cap, which must be expressible
without emptying every result) and is applied in `_execute_calls` BEFORE the
conversation, the journal, the trace row, or the next model request sees it.
Capping at render time is too late - the uncapped text is already stored, and
tool output is the dominant context cost in a real session. Head kept, tail
dropped, marker always present, receipt says what was lost.

`bound_fanout_output` bounds the COMBINED return of a turn
(`max_tool_fanout_chars`, 24000): per-result caps do not bound a fan-out.
Over-budget results are REPLACED BY A BOUNDED NOTE, and the receipt counts
`truncated` / `replaced` separately so a withheld result is never mistaken for
an empty one.

### 5. Config keys (all in `DEFAULTS`, all behaviour-changing)

`max_repeat_tool_calls: 3`, `loop_guard_read_only: False`,
`max_tool_fanout: 8`, `max_tool_fanout_chars: 24000`,
`tool_output_token_limit: 4000`, `search_max_matches: 50`,
`search_sample_matches: 10`, `search_max_files_scanned: 5000`.

These ARE in `DEFAULTS` (unlike most rounds' keys) because AGT-04 specifies
the values as the defaults - a default that is not the default is not a
default. The consequence is stated: every task and every eval arm now gets the
bounded search, the bounded fan-out and the storage-time cap.

### 6. Verification (real, this tree, `-p no:randomly`)

- **NEW `tests/test_agt_04_search_budgets.py` -> 42 passed, 1 skipped** (a
  Windows symlink-privilege case). Named after behaviour, not implementation:
  a 500-match search errors and stays bounded; the render is under 1200 chars;
  the cap is not buyable with `max_results`; a hostile query never raises; a
  symlinked component is not searched; no catalog tool offers a paging argument;
  the search signature has no paging parameter; a paging argument is refused for
  five different names; a stringly-typed class name cannot make a write
  concurrent; four 150 ms reads finish in under 0.30 s while four 150 ms writes
  take over 0.55 s; the fan-out budget bounds the total; the combined return is
  bounded; the repeat detector fires at exactly the 4th identical call; a
  pre-checked fingerprint is not observed twice; four different reads in one
  turn do not trip it; a repeated call becomes `needs_input` with
  `identical_count: 4` and `threshold: 3`; the stored output, the trace row and
  the `tool_output_capped` receipt all carry the capped text; and two tests
  assert the verifier gate is untouched.
- **The strongest proof is end to end through the REAL kernel**: a scripted
  model re-emitting the same canonical call every turn makes the run end
  `needs_input` with exactly one `doom_loop_detected` row
  (`identical_count: 4`, `threshold: 3`) and exactly THREE `tool_result` rows -
  the fourth call did not execute. The control runs four reads across two
  DISTINCT files, exceeds the repeat count of any single call, and reaches its
  normal end with no detection.
- `test_agt_04 test_tool_protocol test_agent_kernel test_retrieval_tools
  test_config_trace_state test_ceiling05_knowledge` -> **186 passed, 2 skipped**.
- `test_ceiling_r2_09_scale test_recovery_steering test_workspace_security
  test_ceiling_r2_07_codemod` (run before the two shared-file collisions
  described below) -> **287 passed, 2 skipped**.
- `test_agent_loop test_evals_run test_evals_tasks test_daily_driver_evals
  test_ceiling_r2_04_daily_default test_ceiling_r2_07_codemod` ->
  **232 passed, 1 failed**. The failure is
  `test_daily_driver_evals.py::test_quick_matrix_runs_real_comparison_with_receipts`
  (`status: failed` vs `complete`) in a SIX-FILE GROUP run. **It PASSES
  standalone (155 s) and the whole 34-test file passes standalone (250 s).**
  Reported as observed, not as exonerated: this is the documented host-load
  flake class in this tree, and no assertion was weakened to hide it.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0 (re-run after the
  final edit).
- `ruff check` clean on all six owned files plus the new test; `compileall`
  clean.
- **No Docker lane and no live-provider lane were run.** Every model call in
  this suite is a scripted double.

**Two concurrent-edit collisions with other terminals, recorded rather than
repaired** (per the shared-tree rule: never revert another module's in-flight
work). `harness/editor.py` was mid-write and unparseable for about four minutes
(18:00-18:05), and `harness/tools.py` had a half-landed `lint_mod` reference
for about three minutes (18:14-18:17). In both cases the owning terminal's
write completed on its own and the affected suites went green with no repair and
no revert here. **If you see a `NameError: name 'lint_mod' is not defined` or an
`IndentationError` in `editor.py`, that is this, not a regression.**

### 7. Not implemented / honest notes

- **The legacy bash-only fix loop (`harness/core.py`, `harness/agent_loop.py`)
  is UNCHANGED and has none of this.** The bounded search, the fan-out and the
  doom-loop decision are on the typed-kernel path. The cross-terminal request is
  to route `harness/agent_loop.py`'s `GREP`/`BATCH` through `search_repo` and
  give its `ToolLoopGuard` the same escalate-to-user treatment; that file is not
  this round's.
- **`web_search` (the catalog's `network` tool) is NOT bounded by
  `search_max_matches`.** It is a network tool with its own `limit` argument and
  its own backend, and a different class of result (a provider's ranking, not a
  repository grep). Bounding it needs the handler's own limits.
- **The fan-out is bounded per TURN, not per RUN.** 8 calls a turn with a
  50-turn run is 400 calls; `max_tool_recoveries` and the per-call cap bound the
  rest. There is no run-level fan-out total.
- **`cap_tool_output` keeps the HEAD and drops the tail.** For a pytest-shaped
  result the answer is at the end. The kernel's `shape_tool_output` (head+tail,
  tail-biased) runs in the handler and this cap re-slices the shaped text, so for
  a very large result the tail can still be lost. Not measured; flagged rather
  than claimed.
- **The `grep` handler's `path` argument now resolves through
  `retrieval._safe_source_path`, which refuses a path whose size exceeds
  `_LARGE_FILE` (200 KB).** The old handler would have searched it. A narrowing,
  not a regression, but still a change.
- **`_iter_bounded_files` enumerates `max_files * 4` candidates** so a narrow
  `glob` over a large tree still finds its files. On a tree whose first
  4x-file-cap candidates do not contain the glob, the walk is reported
  truncated rather than silently short.
- **`harness/agent_kernel/kernel.py` carries one `ruff` finding
  (`typing.Sequence` unused) that is NOT this round's** - another terminal's
  in-flight edit. Left alone.

## AGT-02 — the bounded reflection loop (2026-09-28)

**Owns `harness/tool_errors.py` (one new section), the `_run_agent_legacy`
loop body in `harness/agent_loop.py`, three `DEFAULTS` keys, and NEW
`tests/test_agt_02_reflection.py`. `harness/core.py`,
`harness/agent_kernel/**`, `cli/**` and every verifier were NOT edited.**

A failure that does not become the next turn's input is a failure the model has
to rediscover. This round makes it the input, under a hard cap, with honest
retry classes — and, in the process, found that the legacy loop could not tell
a *failed call* from a *command that ran and reported failure*.

### 1. One loop, two caps, both reported

Every failure the legacy loop sees now goes through ONE seam
(`_reflect`, `harness/agent_loop.py:1553`) and becomes the next user message
**verbatim**, with its evidence attached:

| failure site | kind | class |
|---|---|---|
| reply is not a tool call | `malformed_tool_call` | `model_error` |
| a tool call failed (no result) | from `_recovery`, else `_tool_result_kind` | `task_error` |
| VERIFY failed | `verification_failed` | `task_error` |
| approval required and not given | `command_rejected` | `policy_refusal` |
| every path in a command is forbidden | `permission_denied` | `policy_refusal` |
| a command the deny-guard refused | `command_rejected` | `policy_refusal` |
| same command repeated | `loop_detected` | `terminal` |
| provider fault that survived `max_model_attempts` | the provider's own kind | `transient_provider` |

- `reflection_max_per_step` (**3**) bounds **consecutive** failures: a
  successful turn calls `note_success()` and ends the streak. This is the
  "three failures, then a fourth is refused" cap.
- `reflection_max_per_run` (**12**) bounds the whole run. 4x the step cap:
  a run may recover from four separate failure streaks, and a fifth is refused.
- Both are in `DEFAULTS` deliberately. An unbounded reflection loop IS the
  defect, so a cap that only exists when asked for is not a cap. The
  `tests/test_ceiling_r2_06_editing.py` / `test_ceiling_r2_07_codemod.py`
  "nothing behaviour-changing in DEFAULTS" pins are about EDIT/codemod keys and
  do not cover these three; the daily-driver matrix below is the measured
  blast-radius evidence for them.
- **Reported** in the existing `recovery_stats` journal row (a new `reflection`
  key — `policy`/`model` are unchanged), on the result as
  `AgentResult["reflection"]` + `AgentResult["end_reason"]`, and in the
  per-failure `reflection` journal rows.
- A run that spends its budget ends `status="failed"` with an `end_reason`
  naming the cap, the counters and the last failure. It never retries silently
  and never stops without a reason.

### 2. The classification is DERIVED, not a second table

`retry_class_of(kind)` projects an already-classified kind onto the retry axis.
It reads the existing `POLICY` action slugs (`avoid_shape` / `forbid_path` are
the refusal rows) and reuses `classify_model_failure`'s own two frozensets for
the provider classes, so the two functions cannot disagree about a 429. An
**unrecognised kind is `task_error`, never a refusal**: guessing a refusal
would silently disable recovery for a failure nobody has classified yet.
`test_every_existing_kind_maps_to_exactly_one_retry_class` is the gate.

### 3. A refusal is free, and cannot be laundered

A non-retryable failure is journalled with `charged=False` and leaves both
counters untouched — so **a refused command cannot be used to exhaust a run's
recovery budget**, and a transient provider fault (which is retryable) does spend
it. The ordering matters and is pinned: a non-retryable failure is answered
*before* either cap is consulted, so a cap can never reclassify a refusal as a
budget problem. `render_reflection` gives a refusal its own shape — the
evidence plus an explicit "the decision stands", never an invitation to retry
the refused call in another wording.

### 4. The behavioural correction this round found (read before touching BASH)

**A non-zero exit is not a failed call.** The legacy live-BASH path returned
`ok=False` for *any* non-zero exit, so `python -m pytest` reporting a failing
test was classified as a tool error — precisely the class a reflection loop
would spend budget on, on a command that worked exactly as intended.
`_run_bash_live` now:

- keeps the historical `exit=N` shape **byte-for-byte** (the classifier's
  diagnostic prefix still rides it, so nothing that parsed it broke);
- reserves `ok=False` for a call that produced no RESULT — the deny-guard
  (`PermissionError`), a bad path, a refused edit, a raised harness exception;
- treats a **timeout** (`timed_out` or exit 124) as the one non-zero exit that
  is an infrastructure limit rather than a result, shapes it through the shared
  `shape_tool_output`, and reports it as a failed call.

`tests/test_agent_loop.py` (62) and `tests/test_recovery_steering.py` (67) are
unchanged and green, which is the evidence that the success shape is intact.

### 5. The provider-retry branch changed behaviour, deliberately

A provider fault that survives `max_model_attempts` used to end the run. It is
transient, so it is now a charged reflection and the run gets another turn;
only a TERMINAL failure (auth / bad request / a harness `TypeError`) still ends
the run, and it is not reflected on and spends no budget.

### 6. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_agt_02_reflection.py` -> 22 passed.** Three failures then
  a fourth refused (per-step); the per-run cap binding across four *separate*
  failure streaks; a policy refusal costing zero while three malformed replies
  each get a full reflection; a transient fault charging three; a terminal model
  failure neither reflected nor re-asked; the cap reported in the receipt, the
  result and the journal; every reflection carrying its class and its
  verbatim evidence; the refusal carrying no retry invitation and the refused
  call still refused on re-issue; the anti-second-table derivation; and the
  verifier pin (an exhausted run is `failed`, an unverified completion is still
  `completed_unverified`, and the reflection machinery is absent from the DONE
  branch by a source-level check).
- `tests/test_agent_loop.py` + `test_recovery_steering.py` + `test_tool_errors.py`
  + `test_config_trace_state.py` -> **169 passed**.
- `test_agent_kernel` + `test_modes` + `test_ceiling_r2_04_daily_default` +
  `test_cli_runview` + `test_cli_tracelog` -> **307 passed**.
- `test_ceiling14_resilience` + `test_steering` + `test_workspace_security` +
  `test_tool_protocol` -> **192 passed**.
- `test_agent_kernel` + `test_daily_driver_evals` + `test_evals_run` +
  `test_evals_tasks` -> **116 passed**.
- `python -m evals.run --check` -> **14/14 CLEAN**.
- `python -m evals.run --suite daily-driver --no-docker --json` ->
  **52/52 case arms, 26/26 valid comparisons, 28/28 feature-evidence arms,
  `zero_false_verified_successes=true`, `zero_lost_edits`,
  `zero_unauthorized_mutations`, cost $0.0246** — identical to the recorded
  pre-round baseline, which is the measured answer to "does a new cap
  truncate healthy runs". Verdict `NOT_READY`, reported honestly: the Docker
  lane, the live-provider lane and the sampled manual evidence were not
  selected. Report:
  `logs/evals/20260928-185706-ec6d7969819644699475f66abf37cda6/daily_driver_report.json`.
- `ruff check` clean on all four files; `compileall` clean.
- **One red result observed and attributed, not weakened.**
  `tests/test_daily_driver_evals.py::test_new_daily_quality_cases_pass_both_arms[dd_25_checkpoint_hard_kill-*]`
  failed once inside a 4-file combined run (its two child processes have a 30 s
  budget; under load the seed child exited non-77). It passed standalone
  (1 passed), the whole file passed (**34 passed**), and the identical 4-file
  command re-run passed (**116 passed**). `python -m evals.daily_driver --case
  dd_25_checkpoint_hard_kill --arm baseline` passes standalone with all six
  assertions true. Nothing in this round's diff reaches the kernel path that
  case drives, and `evals/AGENTS.md` already records this case as a known
  host-load-sensitive one. **Not counted as a pass in the combined run.**
- **No Docker lane and no live-provider lane were run** and neither is claimed.
  Every model interaction in the new suite is a scripted double through the
  documented `harness.deps.set_call_model` seam, and the failing-BASH proof uses
  the repository's REAL local subprocess boundary rather than a fake sandbox.

### 7. Not implemented, stated plainly

- **The daily/kernel path has no reflection loop.** `harness/agent_kernel/strategy.py`
  is AGT-05's file; it keeps its own `max_tool_recoveries` /
  `max_model_failures` accounting. `ReflectionBudget` is deliberately
  reusable there (it has no agent_loop dependency) — the wiring is one call
  site and is filed below.
- **`harness/core.py`'s fix loop has no reflection loop** either. Its
  `last_feedback` seam already carries verifier evidence between attempts, so
  the gap is the *cap and the class*, not the feedback. Same reusable object.
- **Lint failures are not a distinct class here.** The legacy agent loop has no
  lint gate; its lint-equivalent is the post-edit syntax check inside
  `EDIT`/`WRITE`, which arrives as a normal failed call (`syntax_error`,
  `task_error`). The kernel's `lint_gate` is a different file.
- **A DONE whose verifier fails still ends the run** (status `failed`). The
  cap does not turn a failed final verification into extra attempts: that would
  be a different, larger decision about the fix loop, and the brief's verifier
  header forbids making the completion path looser.
- **The three early-return paths** (empty request, snapshot failure) return
  before the budget exists, so those results carry no `reflection` key. The
  loop never ran, so there is nothing to report.

### 8. Cross-terminal requests

1. **`cli/runview.py` — the `reflection` event is not in `EVENT_VOCABULARY` or
   `INFORMATIONAL_EVENTS`,** so a live TUI run renders it as an unmapped kind
   (the `cli/tui.py:2876` warning). This is the same class as the
   `model_delta` defect UXP §6 recorded, and it is not new to this round: the
   legacy loop already emits `tool_recovery`, `command_recovery`,
   `command_refused`, `loop_guard`, `recovery_stats` and `approval_decided`,
   none of which are classified either. The one-line shape is to add
   `reflection` (and ideally the other five) to
   `EVENT_VOCABULARY["error_retry"]`, or to `INFORMATIONAL_EVENTS` for the
   receipt-style ones. **`cli/runview.py` is not this round's file and was not
   edited.**
2. **`harness/agent_kernel/strategy.py` (AGT-05) — reuse, do not re-implement.**
   `tool_errors.ReflectionBudget` +
   `tool_errors.retry_class_of(tool_errors.classify(...))` is the whole
   mechanism; the daily path's own `max_tool_recoveries` / `max_model_failures`
   accounting can be reported through `budget.report()` beside its own
   counters. Please do not add a second retry-class table there — the
   derivation from `POLICY` is what keeps the two paths from disagreeing about
   a refusal.
3. **`harness/core.py` — same reuse request** for the verifier-gated fix loop.
   Its failure vocabulary already goes through `tool_errors`; it needs the cap
   and the class, not another classifier.
4. **AGT-10 owns `harness/agent_loop.py` next** (queued-message injection at
   the batch boundary). The regions this round touched are the failure seams
   (`_reflect` at ~line 1553 and its five call sites) plus
   `_run_bash_live`; nothing else in the file moved. A queued message should
   ride the same `messages` list the reflections already use, and must not
   consume reflection budget — a queued message is not a failure.

## VEX-CEILING-R2-09 - the retrieval path is bounded and says so (2026-09-27)

**Owns changes in `harness/retrieval.py` + `harness/knowledge.py`. Did NOT
edit `harness/core.py`, `harness/agent_loop.py`, `harness/config.py`,
`harness/agent_kernel/**` or `memory/**` (Terminal 4's files, edited only
under the R2-09 file list).**

**A slow answer and a wrong answer are not the same failure, and this round is
about which one the retrieval path returns.** Before it: end-to-end retrieval
on this repository **did not finish inside a 40-minute budget** (the brief
recorded >600 s). After: a budgeted retrieval returns in **7.3 s at a 5 s
budget** and **30.3 s at a 30 s budget** (within 0.3 s of the budget), always
with `truncated: true` and the list of what was not searched. The four
required proofs are in `tests/test_ceiling_r2_09_scale.py` (16 passed, 1
Windows symlink-privilege skip).

### 1. The four-key result is untouched; truncation lives in a receipt

`retrieve_context` still returns exactly `{terms, files, greps, strategy}`
- `tests/test_retrieval_tools.py::test_retrieve_context_shape` asserts the
exact key set and is unmodified. New public surface:

- `RetrievalOutcome(result, receipt)` with `.truncated` and `.to_dict()`
- `retrieve_context_budgeted(...) -> RetrievalOutcome` - the real
implementation; `retrieve_context` is now a thin wrapper over it and gained
ONE additive keyword-only parameter, `budget_s`
- `walk_code_files_budgeted(...) -> (files, not_searched)`
- `rank_symbols_with_receipt(...) -> (rows, receipt)`; `rank_symbols`
delegates to it and gained keyword-only `frontier` / `budget_s` /
`receipt` (its list return is unchanged)
- `sparse_pagerank(...) -> (ranks, receipt)`
- `load_code_graph(..., *, receipt=None)` - the index's digest provenance
- Truncation labels: `TRUNCATION_COMPLETE | BUDGET | FRONTIER | SPARSE |
ERROR` (`complete` is the only value that may be presented as a whole
answer)

A truncated ranking is **never cached**: serving a partial ranking to a later
call as a complete one is the exact failure the label exists to prevent
(`context_cache_stats()["stores"]` is asserted to stay 0 in that case).

### 2. What the budget bounds, and the one thing it does not

Checked between stages (terms, cache revalidation, structural scores, grep
scores, per-file greps) plus a deadline INSIDE the two whole-repository
scans (`walk_code_files_budgeted` and the full-text grep loop, both checked
per directory and every 64/16 files). The single stage that cannot be
interrupted is a **cold index build** (`CodeGraph.load_or_build`), because a
half-built index is a wrong index; its measured cost is reported in
`receipt["stages"]["structural"]` rather than hidden. On a warm index that
stage is ~9.8 s on this repository.

**The budget keeps the ranking it already computed.** An earlier version
removed files whose line-level greps had not run, which threw away the
best-ranked results - the opposite of the brief. Now those files stay ranked
and the receipt names them in `greps_missing_for` / `not_searched_files`,
and the `strategy` string carries `[truncated: <label>]`.

`not_searched_known` distinguishes "0 candidates were left" from "the count
was not measured" - on the exhausted path the candidate count is deliberately
NOT measured, because counting it means another full walk of the tree, which is
the cost the budget just refused to pay.

### 3. Sparse ranking, and an honest mode

`_pagerank` was **O(V^2) per iteration** (it recomputed
`sum(outgoing.values())` inside the (target, source) double loop). It is now
O(V + E) with the out-totals computed once: **3.90 s -> 0.0083 s at 400
vertices, 17.75 s -> 0.0187 s at 800, 69.16 s -> 0.0417 s at 1,600, 298.17 s
-> 0.1186 s at 3,200** (1,659x at 1,600; the curve is linear now). The
per-edge arithmetic is unchanged, so the ranks are the same numbers.

`sparse_pagerank` seeds from the query's own evidence (terms, target test,
selected/changed files) and rank-walks outward, with an optional `frontier`
bound. `mode` and `restricted` describe the **result**, not the argument:
a bounded walk that happened to reach every vertex is `full`/complete, and a
query whose seeds reach only part of the graph is `sparse`/`truncated`.
That means the DEFAULT `rank_symbols` call on this repository now reports
`truncated: true, truncation: "sparse", vertices_ranked: 387 of 12,750` -
which is the honest description: it stopped running a whole-graph iteration
for a query, and the receipt says so.

### 4. Two defects found by measuring, both fixed

- **The structural layer rebuilt the whole index on every call.**
  `_structural_scores` built into a fresh `tempfile` directory when
  `index_root` was None, re-parsing the entire repository per retrieval
  (measured 33-45 s per call) and discarding the result. The comment
  justifying it - "CodeGraph's own default root lives INSIDE the repo" - was
  STALE since Ceiling-03: the default is `harness_home()/code-graph`, outside
  the repository. It now resolves through `load_code_graph`, so the
  persistent content-digest cache VEX-CEILING-09 introduced is actually used,
  and the never-mutate guarantee is unchanged
  (`test_structural_never_writes_into_original_repo` still passes).
- **The grep layer's walk was unbudgeted and is the dominant cost.**
  `_walk_code_files` resolved containment with `_safe_source_path` per
  file, i.e. an O(depth) `is_symlink` walk plus a `resolve()` per
  candidate. It is now one `os.scandir` walk reusing Terminal 4's hoisted
  `PathSafety`, and it is budgeted.

### 5. KnowledgeContext (the run-scoped binding)

- `retrieval_budget_s` property - read by KEY MEANING, never truthiness: an
  absent key means "no budget" (the historical behaviour), an explicit `0`
  is a measurable OFF arm, an unusable value degrades to "no budget" plus a
  warning. **`retrieval_budget_s` is deliberately NOT in
  `harness/config.py` `DEFAULTS`** - a default there is merged into every
  task and every eval arm, so adding one would silently switch all of them.
- `retrieve(...) -> RetrievalOutcome` - never raises (an unimportable
  retrieval module produces a typed labelled outcome, not an exception), emits
  `retrieval` / `retrieval_truncated`, and counts
  `stats["retrievals"]` / `stats["retrievals_truncated"]`.
- `retrieval_receipt` property; the receipt is in `as_dict()` and
  `close()` reports `retrieval_truncated`.
- `render_truncation_note(outcome) -> str` - the model-facing line, e.g.
  `[RETRIEVAL TRUNCATED: budget] 25995 candidate(s) were not searched ...
  treat the files above as the best available, not the whole repository`.
  It returns `''` for a complete result and never raises, so a caller can
  pipe anything through it.

### 6. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_ceiling_r2_09_scale.py` -> 16 passed, 1 skipped** (the
  skip is the Windows symlink-privilege case). The four required proofs plus:
  the 300k-file path-safety check (env-gated on `NEO_R209_SCALE=1`; the
  gated run was executed and measured **1.592 s for 300,000 files**, 5.31
  us/file, against a 0.526 s enumeration floor), the stat-freshness skip and
  its reported provenance, sparse == full on a graph where the seeds reach the
  whole ranking (exact ranks AND exact order), frontier truncation, the
  linear-not-quadratic PageRank bound, budget exhaustion returning partial
  labelled results without raising, the trace receipt, the no-caching-of-partial
  rule, the budgeted walk, and the `_caller_id` oracle.
- `tests/test_retrieval_tools.py` -> 27 passed, 1 skipped (unchanged, and
  its exact-four-key assertion still passes).
- `tests/test_code_graph.py tests/test_multilang_graph.py
  tests/test_decision_store.py tests/test_mcp_server.py tests/test_mcp_client.py
  tests/test_mcp_stdio_fileno.py tests/test_mcp_adversarial.py
  tests/test_retrieval_tools.py tests/test_ceiling05_knowledge.py
  tests/test_prompt_cache_cost.py` -> **224 passed, 3 skipped** (skips are
  Windows symlink-privilege cases).
- The scale + retrieval + graph + knowledge + cache-cost selection -> **152
  passed, 3 skipped**.
- `python -m evals.run --check` -> **14/14 CLEAN**.
- `ruff check` clean on all four touched files plus the new test.

### 7. Not implemented / honest notes

- **The 30 s budget returned an EMPTY file list on this repository** at one
  measurement, because the structural stage (9.8 s warm) plus the grep stage
  (20.2 s) consumed the budget before the per-file line greps. The answer is
  correct and labelled (`truncated`, `not_searched: 25995`), but on a
  289k-file tree a 30 s budget is simply small; the measurement is reported as
  it happened, not as a good result.
- **Measurements on this repository are noisy** because parallel terminals were
  editing the tree throughout, so the code-graph index rebuilt on most calls
  (33-45 s). The ratios and per-file costs are stable; the absolute end-to-end
  numbers are not. A quiet-tree re-measurement was not possible inside this
  round's window.
- **The ranking path is still name-based over-approximation** (Terminal 4's
  documented contract). Nothing here makes call resolution type-accurate.
- **No embeddings.** The ranking is still lexical + structural, as documented
  in VEX-CEILING-05.
- **A caller that renders only `result` still renders a partial answer** if
  it ignores the receipt. The mitigation is the receipt plus
  `render_truncation_note`; no existing call site was rewritten to render
  the note, because the call sites (`core.py`, `agent_loop.py`) are other
  terminals' files. Filed below.

### 8. Cross-terminal requests

- **harness/retrieval.py::_SKIP_DIRS is the largest remaining retrieval cost
  and widening it is a retrieval-QUALITY decision, so I did not take it.**
  Measured: the same walk with the code-graph skip set opens **155 directories
  in 0.42 s**; with retrieval's set it opens **124,774 in 144.6 s** (1.16 ms
  per `os.scandir` open on this host). The unpruned trees in this repo are
  site-v1-backup, logs, probe_logs, Temp, pip, Microsoft,
  .shots, graphify-out. ~345x on end-to-end retrieval; needs the
  retrieval-semantics owner.
- **Call sites that should render the truncation note and can set the budget**
  (their files, not mine): `harness/core.py`'s planning step and
  `harness/agent_loop.py`'s first request should pass
  `retrieval_budget_s` in the task config and, when
  `knowledge.retrieval_receipt["truncated"]` is true, inject
  `render_truncation_note(outcome)` into the prompt. Until they do, the
  budget is opt-in and the note is never rendered by a production path.
- `harness/config.py`: a `retrieval_budget_s: None` entry would be
  behaviour-neutral (`None` already reads as "no budget") and is
  recommended for discoverability - but it is NOT mine to add, and a value
  there would be a behaviour change.
## VEX-CEILING-R2-15 — the kernel's policy: no `global` bypass, no blank prefix, a boundary (2026-09-27)

**Owns `harness/agent_kernel/policy.py` + the `harness/config.py` keys. The
full round (including the CLI wiring and the shared implementation) is
documented in `cli/AGENTS.md`; this section is what the harness owner needs.**

### 1. `DEFAULTS["agent_process_sandboxed"]` is now `True` — deliberately

This is a **behaviour-changing default** and it IS the fix: the daily
interactive path was running shell commands on the live host, so the path a
user trusts least had the weakest boundary. It is read in **exactly one place**
(`harness/agent_kernel/strategy.py`'s `shell` handler, passed straight to
`SafeToolBackend.execute(..., sandboxed=...)`); a test reads that file and fails
if a second reader appears, because a second reader is a second boundary
decision.

Measured blast radius before the flip: the 4 kernel/daily suites were **148
passed before and after**, the 3 CLI parity/command/resilience suites **171
passed after**, and `evals.run --check` **14/14 CLEAN after**. Real-Docker
end-to-end, same backend the daily strategy builds: default
`profile=docker` in 3.0s vs opt-out `profile=local_trusted` in 0.3s.

**Opt-out:** `daily_sandbox = false` (new key, `None` in `DEFAULTS` so a merged
default cannot be told from a deliberate choice). `shared.approval.resolve_daily_trust`
reconciles it with this key and reports the **weaker** boundary on a conflict,
so the receipt can never overstate containment. Any other consumer reading this
key directly will see `True` and should read the resolver instead.

### 2. `harness/agent_kernel/policy.py` — three fixes, all fail-closed

- **`global` is deleted.** `ApprovalGrant.matches` used to `return True` before
  looking at anything, so one grant defeated the entire policy. The scope
  vocabulary now comes from `shared.approval` (`once`, `exact_call`,
  `session_path`, `session_command_prefix`) and `global` resolves to `once` with
  the reason preserved in `RETIRED_APPROVAL_SCOPES`. `normalize_scope` here keeps
  its signature and now delegates.
- **An empty command prefix is refused** at `PolicyRule.__post_init__`,
  `ApprovalGrant.__post_init__` and `PolicyEngine.record_approval` (which returns
  a **deny** whose reason is the refusal — a bad scope answer must not crash an
  approval prompt). Defence in depth: `matches` returns `False` for a
  force-mutated blank prefix. Note the rule-level check is `None`-tolerant —
  an **absent** prefix still means "this rule does not constrain commands".
- **`_prefix_matches` delegates** to `shared.approval.command_prefix_matches`.
  The local version was a bare `str.startswith`, so `git sta` covered
  `git stash`. One implementation, imported not forked.

Two smaller corrections found by the new suite: `record_approval` no longer
appends a `once` grant (it could never match anything, and it made the grant
list disagree with what was actually remembered), and when a retired scope is
narrowed the decision's `reason` says **"approval accepted once only; scope
'global' is not a scope: …"** instead of a bare "approval accepted", so the
audit log cannot hide what happened.

### 3. Verification

`tests/test_ceiling_r2_15_trust.py` → **49 passed**. `test_agent_kernel` +
`test_ceiling_r2_04_daily_default` + `test_tool_protocol` +
`test_workspace_security` + `test_ceiling_security` + `test_difficulty_approval`
+ `test_config_trace_state` + the new file → **267 passed**.
`test_daily_driver_evals` + `test_evals_run` + `test_evals_tasks` → **69 passed**.
`evals.run --check` 14/14 CLEAN. `ruff check` clean on both owned files;
`ruff format` applied to `policy.py` only (`config.py` holds pre-existing
whole-file debt and was not swept).

### 4. Cross-terminal requests

1. **`harness/agent_loop.py:787-795`** still imports `harness._stubs.sandbox` and
   runs BASH through the **local subprocess stub**, unconditionally, for the
   `legacy_agent` engine. R2-04 made `daily` the default, so the interactive path
   is fixed by this round; `legacy_agent` is not. It needs the same resolved
   boundary read, and flipping it must **fail loud** when Docker is down (the
   documented sandbox contract) — not fall back to the host.
2. **`harness/agent_kernel/strategy.py` — the other process entry points.**
   `shell` now reads the containment key; `process`, `process_write_stdin`,
   `process_kill`, and the `start_local_execution` fallback at `strategy.py:2280`
   still resolve to local execution. They are not reachable from the `bash` tool
   today, so nothing is wrong now, but one `_process_sandboxed(cfg)` helper would
   make the containment story complete.
3. **The other process tools are out of this round's file ownership**; no
   change was made to `strategy.py`.



**Group 0b, run alone. Owns `harness/agent_loop.py` (dispatch region only — NOT
`harness/agent_kernel/*`), `evals/daily_driver.py`, `harness/config.py`.**

### 1. The defect, and the ONE place the decision is now made

`harness/agent_kernel/kernel.py:660` has always returned `("daily", "default")`
from `resolve_agent_strategy`. `harness/agent_loop.py:979` handed it
`explicit="legacy_agent"` whenever the caller named no strategy — the common
case. So the kernel's default was unreachable, and the docstring 25 lines above
the call ("The kernel is the single authority for which strategy runs") was
contradicted by the call itself.

**Resolved in favour of the kernel being the authority** (the prompt's first
option), and it now says so in ONE place:

```python
resolve_agent_dispatch(config) -> (dispatch, name, source)
```

`harness/agent_loop.py:resolve_agent_dispatch` is the single authority for the
**dispatch** decision. It does **not** re-implement the resolver's precedence —
it *calls* `harness.agent_kernel.resolve_agent_strategy`. `dispatch` goes to
`SessionController.run_turn(strategy=...)` and is `None` whenever this module has
no opinion, which is what "the absence of a strategy is absence" means in code.
`run_agent` was re-read after the edit and contains no `explicit=` at all
(pinned by `test_run_agent_does_not_override_the_kernel_resolver`).

**Precedence, and it is the only precedence here:** a strategy the caller NAMED
(`agent_strategy`) beats the compatibility DEFAULT (`agent_default_strategy`).
*This was a real bug in my own first version* — the compat key was checked first,
so a key named "default" could overrule an actual choice. Fixed, and pinned both
ways in `test_compat_default_does_not_shadow_an_explicit_agent_strategy`.

### 2. `legacy_agent` is a compatibility SURFACE, not a fallback

Reachable three ways, all explicit: `agent_strategy="legacy_agent"`, the
`agent_default_strategy` config key, and the new public
**`run_agent_legacy(request, repo_path, config=None, **kwargs)`**, which forces
the pin and forwards everything else verbatim. The legacy engine is fully
supported and its whole feature set (the `{"tool": ...}` reply protocol, `orig/`
undo, FETCH/MCP/plugin verbs, live BASH, legacy `kind`-named trace rows) is
unchanged.

### 3. Config key — `agent_default_strategy` (`harness/config.py`, value `None`)

`None` / `""` / `"none"` / `"default"` all mean "no override", so the `DEFAULTS`
entry is **behaviour-neutral** and adding it switched no task and no eval arm
silently. Only a PRESENT, non-empty, resolvable name changes behaviour (key-
presence detection, per the R2-04 rule). An unresolvable value **raises** from
the resolver rather than falling back, so a typo cannot quietly restore the wrong
engine. Same precedent as R2-03's `test_config_change`.

### 4. The source string is surfaced — and one honest deviation

`AgentResult` gains two **additive** keys: `agent_strategy_source`
(`config` | `compat_default` | `default`) and `agent_strategy_resolver`
(`"harness.agent_loop.resolve_agent_dispatch"`). The kernel's `run_started
.strategy_source` / `strategy_selected.source` already existed; what changed is
that they stopped being corrupted (a default run used to record
`source="explicit"` because `run_agent` forged an explicit request).

**Deviation, stated rather than hidden:** for an unqualified run the *kernel*
journal row still reads `source="spec"`, not `"default"` —
`SessionController.run_turn` pre-fills `RunSpec.strategy` with the resolver's
own fallback (`_strategy_name(requested or "daily")`) before the kernel resolves,
so the spec branch always matches. Making it read `"default"` is a one-line
change in `harness/agent_kernel/kernel.py`, which is **not** this prompt's file
ownership. Until then `agent_strategy_source` is the authoritative source, and
both are asserted in `tests/test_ceiling_r2_04_daily_default.py`. Cross-terminal
request filed in §8.

### 5. The measured consequence (this is the part worth reading)

Same process, same scripted model, two runs, artifacts compared:

| run dir contents | default (`daily`) | explicit `legacy_agent` |
|---|---|---|
| `conversation.jsonl` | yes | **no** |
| `turns.jsonl` | yes | **no** |
| `context.json` | yes | **no** |
| `checkpoint.json`, `pristine`, `trace.jsonl` | yes | yes |

`context_budget` also appears in the default run's event kinds. This is pinned
by `test_a_default_run_writes_the_daily_only_artifacts_a_legacy_run_does_not`,
so the "the daily features are now reachable" claim is measured, not asserted
from a comment.

### 6. `evals/daily_driver.py` — the three previously-failing arms, honestly

New shared predicate `_honest_completion(result) -> (completed, worded_as_success)`:
the first value accepts ANY completed status (an unverified completion is the
honest, expected outcome of a path with no verifier gate); the second is the real
guard — the historical `success` word may only appear when `completed_verified`
is behind it, checked on BOTH `status` and `kernel_status`. **The invariant was
not relaxed**; the probes that pinned `status == "success"` were the defect.

| case | decision | why |
|---|---|---|
| `dd_02_dirty_small_edit` | **left on the new default** | every content assertion (dirty-tree preserved, edit applied, stale-edit reconciled, meaningful diff) passes on `daily` in BOTH arms. It is now the matrix's coverage of the new default. |
| `dd_19_skill_discover_show_inject` | **left on the new default** | the compiled-context injection point is shared, so the skill body still reaches the model's own first prompt. |
| `dd_04_interpret_test_failure` | **pinned to `run_agent_legacy`** | its EVIDENCE is a fake at the LEGACY sandbox boundary (`set_execute_sandboxed`); the daily strategy uses `execution.workspace.SafeToolBackend`, so on `daily` the scripted failing-test output never reaches the model and `failure_output_reached_model` is vacuously unobservable. Pinning keeps it testing the claim it was written for. No assertion was relaxed. |
| feature lane `agent_fetch_enabled` | **pinned to `run_agent_legacy`** | same class: the claim IS the legacy `{"tool":"fetch"}` verb + the `web_fetch` audit event. On `daily` the typed catalog's `web_fetch` is a different tool on a different path. The OFF arm keeps its meaning because the off arm has nothing to emit. |

`dd_19`'s reported `ProbeOutcome.status` no longer collapses an honest
`completed_unverified` into `"failed"`; it reports the run's own status.

### 7. Tests that had to change, and why (disclosed — not all were in my file list)

- **NEW `tests/test_ceiling_r2_04_daily_default.py` → 19 passed.** Every test is
  named after the behaviour. Includes the real-journal test
  (`run_started` + `strategy_selected` both name the engine and a non-empty
  source, for the default and the explicit-legacy run), the
  `completed_unverified`-is-never-`success` pair (both engines), the
  `_honest_completion` guard matrix, and the artifacts measurement.
- **`tests/test_agent_loop.py` → 62 passed.** 17 tests were RED on the new
  default. Every one of them unit-tests a LEGACY-loop-only feature (the
  `{"tool": ...}` protocol, `orig/` undo, FETCH/MCP/plugin, live BASH, legacy
  `kind` rows), so the file now pins `LEGACY_AGENT_CONFIG = {"agent_strategy":
  "legacy_agent"}` in its `_run` helper and in its 7 direct `run_agent` calls,
  with a comment saying why and pointing at the new file. **No assertion was
  weakened** — the fix is a request for the engine, not a relaxed expectation.
- **`tests/test_agent_kernel.py::test_default_general_agent_keeps_cli_shape_without_verified_claim`**
  pinned `result["agent_strategy"] == "legacy_agent"` on a *default* run — it
  encoded the exact bug R2-04 fixes. Now asserts `("daily", "default")`. Its
  honest-status assertions are unchanged and still pass.
- **`tests/test_recovery_steering.py::test_loop_guard_stops_the_agent_loop_too`**
  — its own docstring says "The same guard on the legacy agent path" and it
  injects at `set_execute_sandboxed`; pinned by name. All its guard assertions
  (repeats count, `loop_guard` event, `recovery_stats` receipt) are unchanged.

`tests/test_agent_loop.py`, `test_agent_kernel.py`, and
`test_recovery_steering.py` are **not** in R2-04's three declared files. They
had to change or the change would ship red, Group 0b was scheduled to run
alone, and the edits are single-key config pins plus one corrected default-value
assertion. Disclosed here rather than done quietly.

### 8. Verification actually run (this tree, `-p no:cacheprovider`)

- **Group-0a pre-flight (why it was safe to start):** `test_ceiling_r2_02_flake`
  + `r2_03_config_guard` + `r2_05_baseline` → **150 passed**;
  `test_ceiling08_verification` → **78 passed**; `test_verification_gate_wiring`
  → **27 passed**.
- **NEW suite:** `test_ceiling_r2_04_daily_default.py` → **19 passed**.
- **The named gate — full daily-driver matrix:**
  `python -m evals.run --suite daily-driver --no-docker --json` →
  **52/52 case arms ok, 26/26 valid comparisons, 28/28 feature-evidence arms,
  `pass_count 52`, `fail_count 0`, `zero_false_verified_successes`,
  `zero_unauthorized_mutations`, `zero_lost_edits`, cost $0.0246.** Verdict
  `NOT_READY`, reported honestly: the Docker lane, the live-provider lane, and
  the sampled manual-repair evidence were not selected. Before this round the
  same command reported `dd_02`/`dd_04`/`dd_19` failing in BOTH arms.
- **The other named gate — TUI suites:** `test_cli_tui`, `test_cli_tui_layout`,
  `test_tui_contract`, `test_cli_runview`, `test_cli_tracelog` → **234 passed**.
  Plus `test_cli_terminal_parity`, `test_ceiling16_surfaces`, `test_cli_neo3`,
  `test_modes`, `test_cli_command_system`, `test_cli_polish` → **345 passed**.
- **Kernel/evals:** `test_agent_kernel` (47), `test_config_trace_state`,
  `test_daily_driver_evals`, `test_evals_run`, `test_evals_tasks`,
  `test_agent_loop` (62), `test_ceiling_r2_04_daily_default` (19) → **210 passed**.
- **Daily-only machinery:** `test_context_budget_engine`, `test_ceiling05_knowledge`,
  `test_recovery_steering` (67), `test_workspace_security`, `test_tool_protocol`,
  `test_editor_prompts`, `test_adversarial`, `test_coordination`,
  `test_stubs_and_deps`, `test_scheduler_integration`, `test_skills`
  → **373 passed, 2 skipped**.
- **CLI sweep:** `test_cli`, `test_cli_neo`, `test_cli_neo2`, `test_cli_errors`,
  `test_cli_adversarial`, `test_cli_slash2`, `test_cli_session`,
  `test_cli_config`, `test_cli_theme`, `test_cli_fileview`,
  `test_cli_power_tools` → **364 passed**. Sessions/release/theme/ACP/SDK/
  installers/plugins/connectors/onboard group → **335 passed, 3 skipped**.
  SLO/evidence/CI-truth/quality-capability/release-workflows group
  → **174 passed, 1 skipped**. Steering/streaming/extensions/integrations/
  dashboard/MCP/tracing group → **371 passed, 4 skipped**.
- **Real Docker verifier lane:** `test_e2e_run_task`, `test_verify`,
  `test_sandbox`, `test_verify_js` → **157 passed** (13m02s). This is real
  Docker evidence, not a stub.
- `python -m evals.run --check` → **14/14 CLEAN**.
- `ruff check` clean on every file I edited; `ruff format` applied **only** to
  the new test file (shared-file protocol §4.2.3 — no formatter was run on
  `agent_loop.py`, `config.py`, `daily_driver.py`, or any test file I edited
  that another terminal also reads). `python -m compileall -q harness evals`
  clean.

**Two failures observed in a whole-tree `tests/` run, NEITHER mine and NEITHER
weakened** (per the shared-tree rule, stated explicitly):

1. `tests/test_ceiling03_sessions.py::test_five_thousand_indexed_sessions_list_under_100ms_p95`
   — a host-contention p95 wall-clock pin on the session-index **listing** path
   (`cli/session.py` / `memory/paths.py`, untouched by this round). The test's own
   diagnostic reported the host 1.89x slowed. It passed on a standalone re-run of
   the whole file (**31 passed**) and again standalone.
2. `tests/test_ceiling_r2_06_editing.py::test_the_harness_default_leaves_the_typed_kernel_path_lenient_on_purpose`
   — a **concurrent in-flight edit by another terminal**: `harness/editor.py` was
   rewritten at 21:23 and `tests/test_ceiling_r2_06_editing.py` at 21:27, *while
   my test chunk was running*. It passed standalone immediately afterwards
   (0.54s). It exercises `ToolRegistry.execute` with an explicit
   `require_edit_digest: True` and has no interaction with strategy dispatch;
   `agent_default_strategy` is read by exactly one module (`agent_loop.py`),
   verified by grep.

### 9. Not implemented / honest blocked status

- **The kernel journal's `source` for an unqualified run still reads `"spec"`,
  not `"default"`** (§4). It needs a one-line change in
  `harness/agent_kernel/kernel.py::SessionController.run_turn`, which is not this
  prompt's ownership. The strategy NAME is identical (`"daily"`); only the label
  of where it came from differs, and `agent_strategy_source` carries the
  accurate one.
- **No live-provider lane was run** and no credential was inspected, requested,
  or retained. Every model interaction in this round's evidence is a scripted
  double or the repo's existing fixtures.
- **The full `tests/` tree was not run in ONE process.** It exceeded the 2-hour
  command limit, so it was covered in chunks (§8). Every file in `tests/` was
  either run in one of those chunks or is listed above as a host-load /
  concurrent-edit failure that passes standalone. `test_provider_smoke` and
  `test_installed_user_flow` were not selected: the first needs credentials and
  the second builds and installs a wheel, neither of which this prompt needed.
- **`scripts/shadow_gate.py` and `demo/agent_demo.py` call `run_agent` without a
  strategy** and will now run on `daily`. They were NOT exercised this round
  (out of scope, and `demo/` is a presentation surface). If either regresses, the
  fix is a config pin, not a revert.
- **No claim about the daily path's model quality.** The switch is measured by
  artifacts, events, and the matrix — never by "it answers better".

### 10. Cross-terminal requests

- **Kernel owner (`harness/agent_kernel/kernel.py`):** one line in
  `SessionController.run_turn` would make the resolver's `"default"` source
  reachable. Today `selected = _strategy_name(requested or "daily")` pre-fills
  `RunSpec.strategy`, so `resolve_agent_strategy`'s spec branch always matches
  and an unconfigured run records `strategy_source="spec"`. Either leave
  `RunSpec.strategy` empty when nothing was requested, or have the kernel
  record the source it was actually given. `agent_loop` is already correct and
  reports the true source; nothing in `agent_loop` needs to change when this
  lands.
- **`cli/AGENTS.md` owner:** the interactive compatibility path
  (`cli/interactive.py:6452` → `run_agent`) now dispatches to `daily`. The
  explicit-mode path (`cli/interactive.py:6169-6170`) sets
  `agent_kernel_enabled` + `agent_strategy` and is unaffected. If the REPL/TUI
  must keep the legacy engine, pin `agent_default_strategy = "legacy_agent"` in
  the session config — that is now a supported one-key answer, and it should be
  a deliberate decision rather than an accident.
- **Whoever owns the next default switch:** `DEFAULTS` is merged into every task
  and every eval arm. A behaviour-changing value there switches all of them
  silently. `agent_default_strategy` is `None` for exactly that reason.

## VEX-CEILING-R2-06 — editing correctness: ambiguity is a refusal, bytes are preserved (2026-09-26)

**The legacy `EDIT` verb took the first match of an ambiguous target, read
every file as UTF-8 with `errors="replace"`, and left a syntactically broken
file in the work tree when its own post-edit check failed.** This round puts
one primitive in `harness/editor.py` that gets all four right, shares the
kernel's refusal vocabulary, and refuses a mutation the session never read.

### 1. What is built

| surface | what it owns |
|---|---|
| `harness.editor.apply_text_edit(...) -> EditOutcome` | the ONE exact-block edit primitive |
| `harness.editor.EditSession` | the per-run read ledger `require_edit_digest` is enforced against |
| `harness.editor.detect_encoding` / `detect_newline` | the format facts the receipt reports |
| `harness.editor.ERROR_*` / `EDIT_ERROR_KINDS` | the shared refusal vocabulary (three slugs + three documented extensions) |
| `harness.tools.EDIT_ERROR_*` / `edit_refusal_vocabulary()` / `apply_catalog_edit()` | the catalog-facing edit entry point (re-exports, not a second set of literals) |

Four properties, each pinned by a test rather than a comment:

- **Ambiguity is a refusal.** `>1` match is `ambiguous_match` carrying the
  candidate line numbers and context (capped by `edit_ambiguity_candidates`);
  `0` is `no_match`. There is no "replace the first" and no replace-all escape
  hatch. A multi-match edit never touches the file.
- **Bytes are preserved, by construction.** The replacement is a byte splice
  found with `bytes.find`, so untouched content keeps its encoding, its BOM and
  its line endings exactly — including a `newlines="mixed"` file, which stays
  mixed where the edit did not touch. `detect_encoding` resolves a BOM, then a
  PEP 263 cookie, then strict UTF-8, and returns `None` when none of those
  determine it; the edit is then REFUSED (`undetermined_encoding`), never read
  with `errors="replace"`, which is what destroyed latin-1 files.
- **A failed post-edit check rolls back.** The pre-image bytes are captured
  before the write, so a failed `validate` restores the file byte-for-byte and
  the outcome carries `rolled_back=True`. Leaving a known-broken edit for the
  next turn to read is the trap this closes.
- **Writes are atomic.** Every write AND every rollback goes through
  `execution.workspace._atomic_write_bytes` — the repository's existing
  primitive, reused rather than re-invented. `test_the_write_goes_through_the_repository_atomic_write_primitive`
  spies on that exact function and shows the rollback is a second call through
  it, not a `Path.write`.

`EditOutcome` is the receipt: `encoding`, `newline`, `newline_mixed`,
`newline_adapted`, `match_count`, `candidates`, `rolled_back`,
`digest_required`, `digest_relaxed`, `digest_relaxed_reason`, `write_mode`,
`pre_sha256`, `post_sha256`. Every refusal is a **value**, never an exception,
and every one of them is traced (`edit_applied` / `edit_rolled_back` /
`edit_refused`).

### 2. One vocabulary, and a real defect the BOM found

`ambiguous_match` / `no_match` / `stale_read` are **the kernel's literals**.
`harness/agent_kernel/tools.py` is untouched, so the two sets are pinned equal
by `test_the_edit_refusal_slugs_are_the_kernels_slugs` rather than re-imported
— `harness.editor` is the lowest-level of the three modules and must stay
importable without the kernel or the catalog. Three documented extensions
(`undetermined_encoding`, `post_check_failed`, `edit_refused`) are additions,
not renames.

`harness/editor.py::syntax_check` gained ONE additive keyword-only
`encoding=None`. This is not cosmetic: read as plain `utf-8`, a UTF-8-BOM
Python file decodes to U+FEFF and `compile()` reports *"invalid non-printable
character"* for a file that is perfectly valid. Without the parameter the new
primitive would have rolled back **every** edit to a BOM'd Python file — the
rollback mechanism working, faithfully, on a false positive. With it, the check
reads the file the way the edit wrote it. Every pre-existing call site passes no
`encoding` and is byte-for-byte unchanged.

### 3. `require_edit_digest` — strict by default where it is safe, and MEASURED where it is not

Strictness in `apply_text_edit` is a **floor, not a default value**: an absent
key, `None`, or `True` all mean "on"; only an explicit `False` relaxes it, and
the receipt then says `digest_relaxed=True` plus a reason. So every mutating
path through this primitive is on by default and a caller cannot reach the
unsafe behaviour by forgetting to configure anything.

**The global default was measured before it was set, and it is not safe yet.**
`DEFAULTS["require_edit_digest"]` is `None`, deliberately. Flipping it to `True`
— nothing else changed, same suite — gives:

| `require_edit_digest` | daily-driver arms ok | `zero_false_verified_successes` |
|---|---|---|
| `None` (shipped) | **52/52** | **True** |
| `True` | 41/52 | **False** |

Ten arms go red (`dd_21`, `dd_22`, `dd_24`, `dd_25`, `dd_26`, both arms) and
two `tests/test_agent_kernel.py` tests with them. The recorded `dd_22`
`tool_result` is the whole story:

```
TOOL ERROR [stale_read]: edit requires the content digest of the file you read,
and app.py was never read in this run.
```

Root cause: **`strategy._bind_harness_arguments` is not unconditional.** It
returns early when the strategy has no `execution_backend`, and its table
(`_HARNESS_BOUND_REVISIONS`) covers only `edit`/`rename`/`delete`. So a strict
default refuses real mutations — and the run then *finishes anyway*, which is
how `zero_false_verified_successes` goes False. A default that manufactures a
false verified success is worse than a missing default, so the value is `None`
and the blocker is pinned by
`test_the_harness_default_leaves_the_typed_kernel_path_lenient_on_purpose`, which
will start failing the moment someone sets it to `True` — by which time the
binder fix should have landed.

### 4. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_ceiling_r2_06_editing.py` -> 34 passed.** All seven required
  proofs, named after the behaviour, asserted on the STABLE SLUG and on the
  file's BYTES:
  - ambiguous edit → `ambiguous_match`, candidates at lines 2 and 6, message
    names both, file byte-identical, `write_mode == ""`;
  - CRLF round trip: a `\n`-form `old_string` still matches (`newline_adapted`),
    and the result has **5 CRLF and 0 lone LF**;
  - latin-1 round trip: the fixture is proven NOT decodable as UTF-8 first, the
    edit reports `iso8859-1`, the result still contains `\xe9`/`\xf1` and still
    fails a UTF-8 decode;
  - failed syntax check → `post_check_failed`, `rolled_back=True`, file
    byte-identical, and the session ledger re-baselined to the pre-image;
  - **a real crash mid-write**: `tests/editor_crash_driver.py` patches
    `execution.workspace.os.replace` to `os._exit(70)` so the process dies at
    the exact instant the rename would have made the new content visible, and
    the test asserts exit 70 plus a byte-identical original. The stray
    dot-prefixed `.tmp` is expected residue (a real crash cannot run the
    `finally`) and is asserted to never be the target.
  - `require_edit_digest=True` refuses a mutation of a never-read file, and the
    default configuration (no config at all) does the same.
  - plus: `undetermined_encoding` for an undeclared non-UTF-8 file AND for a
    declared-but-unknown cookie, a crashing gate treated as a failed gate, the
    trace receipt on a rollback, a relaxing knob not becoming a general off
    switch (relaxing the digest still leaves the ambiguity refusal intact), a
    `write`-without-`session` in every config shape leaving the file unchanged,
    and `apply_catalog_edit` refusing a traversal path before touching disk.
- Neighbour regression selection (8 files, including the two that were red under
  the measured flip) -> **305 passed, 1 skipped** (the skip is a Windows
  symlink-privilege case).
- `python -m evals.run --check` -> **14/14 CLEAN**.
  `python -m evals.run --quick` -> **40/40, verdict CLEAN, 0 regressions**.
  `python -m evals.run --suite daily-driver --no-docker --json` -> **52/52 arms
  ok, `zero_false_verified_successes=true`**; overall readiness `NOT_READY`
  because the Docker and live-provider lanes were not selected.
- `ruff check` clean on all five owned/added files; `compileall` clean; scoped
  `git diff --check` clean.
- **No Docker lane and no live-provider lane were run and neither is claimed.**
  Every model call in these tests is a scripted double.
- Measurement caveat, stated rather than hidden: the `require_edit_digest=False`
  arm of the A/B above recorded **51/52**, because
  `dd_20_live_tui_status_diff/baseline` failed on that run and passed on the
  shipped run. That one arm is timing-flaky under load; the 52/52 vs 41/52 gap
  and the `zero_false_verified_successes` flip are not.

### 5. Not yet implemented / honest notes

- **The legacy `EDIT` verb is NOT yet routed through the primitive.** The
  mechanism is complete and tested; the call site is in
  `harness/agent_loop.py`, which this prompt does not own. The exact patch is
  in §6.
- **`require_edit_digest` is not globally on** — see §3 for the measurement and
  the fix that unblocks it. Strictness is on for every path through
  `apply_text_edit` / `apply_catalog_edit` today.
- **Ambiguity is counted on exact text, not lines.** A caller that passes a
  one-line `old_string` for something repeated 200 times gets
  `ambiguous_match` with 5 candidates and a count, not a diff. There is no
  "apply to all occurrences", deliberately.
- **The newline adaptation is a fallback, not a rewrite.** An `old_string` is
  tried verbatim first and only then in the file's dominant convention, so a
  genuinely ambiguous target stays ambiguous. It does not normalize a
  `new_string` the caller wrote with the wrong convention *inside* the matched
  block beyond that same translation.
- **`max_file_bytes` is the only size ceiling**, reused from the existing key
  rather than adding an edit-specific one.
- The atomic primitive is reached through a **private** name
  (`execution.workspace._atomic_write_bytes`) because this prompt does not own
  `execution/workspace.py`. It is the SAME function, imported not copied, and
  the spy test proves it — but promoting it to a public helper is a request
  (§6), not something done here.

### 6. Cross-terminal requests

1. **`harness/agent_loop.py` (legacy `EDIT`, ~line 2323) — route it through the
   primitive.** This is the one line of wiring the round is missing. Replace the
   body of the `if tool == "edit":` branch with:
   ```python
   session = _edit_session(agent_state)  # see below
   outcome = editor.apply_text_edit(
       str(repo_p), rel, str(old), str(new),
       session=session, config=cfg, trace=_trace_sink(emit),
   )
   if outcome.ok:
       norm = outcome.path
       if norm not in files_touched:
           files_touched.append(norm)
       emit("edit_applied", outcome.to_dict())
       return True, f"EDIT {rel}: replaced 1 block."
   emit("edit_refused", outcome.to_dict())
   return False, f"TOOL ERROR [{outcome.error_kind}]: {outcome.message}"
   ```
   and build the ledger once per session from the tool results the loop already
   has — `EDIT_SESSION.note_read(rel, data=...)` whenever a `READ` returns, and
   `note_mutation` on every applied edit. The `EDIT` branch's current three
   defects all disappear with this: no more first-match, no more
   `errors="replace"`, and no more broken file left behind (the primitive rolls
   back). Keep `_stash_original` (the loop's own undo reference) — it is
   independent of the primitive's rollback.
2. **`harness/agent_kernel/strategy.py` — make the binder unconditional, then
   `require_edit_digest` can go global.** Two gaps, both in that file:
   `_bind_harness_arguments` returns early when `self.execution_backend` is
   `None`, and `_HARNESS_BOUND_REVISIONS` omits `write`. When both are fixed,
   set `DEFAULTS["require_edit_digest"] = True` and update
   `test_the_harness_default_leaves_the_typed_kernel_path_lenient_on_purpose`
   (it asserts the value is not `True`; the assertion to flip is that one line,
   and the reason to record is §3's table). Re-run
   `python -m evals.run --suite daily-driver --no-docker --json` and require
   `zero_false_verified_successes=true` before believing it.
3. **`execution/workspace.py` — promote `_atomic_write_bytes`.** It is now a
   cross-module dependency of `harness/editor.py`. A public
   `atomic_write_bytes(path, data, mode=None)` (or an `__all__` entry) would
   remove the private import; the algorithm must not be forked into a second
   copy in the meantime.
4. **`evals/daily_driver.py` (T10) — not needed now.** The ten arms that broke
   under a global flip do so because their scripted replies mutate without
   reading. Once request 2 lands, an arm that reads before mutating is the
   stronger fixture; until then, nothing in `evals/` needs changing.
5. **T01 (kernel):** nothing was edited in `harness/agent_kernel/`. The three
   shared slugs in that file are load-bearing for this round and are now
   test-pinned against `harness/editor.py` — please keep the three literal
   values if the catalog is ever renamed.

## VEX-CEILING-R2-08 — kernel model backoff, one context authority (2026-09-26)

**Two recorded, unlanded handoffs are now landed, and a duplicate authority is
collapsed.** The kernel's model-call path had no bounded backoff (Ceiling-07
filed it as a cross-owner request and it never landed, so a 502 could end a
healthy run while the legacy path retried three times); `context_compaction_model`
was read into the receipt while the summarizer used the run's own model; and
`runtime/model_capabilities.py` plus the kernel's own budget keys each resolved a
context window independently, which is how a 32k model gets run with a 128k
budget.

### 1. The kernel retries, with the legacy path's classifier and vocabulary

`harness/agent_kernel/gateway.py` reuses `harness.tool_errors.ModelRecovery` and
`classify_model_failure` — **the same two objects `harness.core.run_step` uses**.
There is no second classifier and no second slug table, so the two paths cannot
drift. Same config keys: `max_model_attempts`, `model_retry_base_s`,
`model_retry_cap_s`. `max_model_attempts: 1` is the OFF arm and emits no
`model_recovery` row at all.

- `ModelGateway.__init__` gains `recovery=None` and `sleep=None`;
  `bind_recovery(recovery)`, `has_bound_recovery`, `recovery_report()` are new.
  A gateway with no bound recovery builds one lazily from its own config, so a
  caller that constructs `ModelGateway(config=...)` directly still gets the
  bounded retry.
- `_invoke_with_recovery` retries on the SAME request (a model call is read-only
  with respect to the workspace) and re-raises, so the caller's single `except`
  is the only place a failure becomes a `ModelResponse` — one code path, not two.
- `KeyboardInterrupt`/`SystemExit` are re-raised before classification. A cancel
  is not a provider fault and must not be retried or relabelled.
- **A non-provider exception is never a provider fault.** A `TypeError` in our
  own code classifies `model_internal`, is TERMINAL, and is raised on the first
  attempt — however provider-flavoured its message
  (`TypeError("upstream returned 502 bad gateway and timed out")` is still
  internal). Both facts are pinned by tests, not by a comment.
- A retried call is still ONE `ModelResponse`; the call-ledger row gains
  `attempts` (provider attempts that logical call cost) and
  `model_failure_kind`, so retries are visible instead of hidden behind one row.
  `ModelResponse.model_failure` carries the classified kind as a JSON-safe map.

`DailyCodingStrategy._bind_model_recovery()` binds ONE recovery per run (so its
counters span the run) writing through `JournalTraceLogger(self.events)`, so the
`model_recovery` rows land on the run's own `trace.jsonl`.
`RunResult.metadata["model_recovery"]` is the machine-readable form, next to the
existing `metadata["context"]`.

**`model_recovery` is now one payload shape, not two.** The gateway's rows carry
`label: "kernel-model-call"`; the daily strategy's pre-existing turn-level row
(`attempt`, `error` — both UNCHANGED) is enriched with `label: "kernel-turn"`,
`scope: "turn"`, `step`, `max_attempts`, `kind`, `detail`, `status_code`,
`retryable`, `terminal`, `backoff_s`, `action`. The two axes are different and
both are real: **within a call** the gateway spends `max_model_attempts` against
one boundary; **across turns** the strategy counts failed turns against
`max_model_failures`. A turn-level give-up row is only reached after the
per-call budget is already spent.

### 2. `context_compaction_model` is honoured, and the receipt tells the truth

It used to be read straight into the compaction receipt while the summarizer ran
on the run's own model: the receipt named a model that never summarized anything.

- `_summary_gateway()` returns `(gateway, model_name)`. When
  `context_compaction_model` names a model other than the run's own, a second
  gateway bound to it is used for the compaction request and nothing else,
  reusing the run's own boundary (`call_fn`/`model_client`) exactly as the
  fallback gateway does.
- **If the run's boundary is not reachable, the key is NOT honoured**: the run's
  own model summarizes and the receipt says so. A summarizer that quietly
  resolved the default provider would be both untestable and unaccounted.
- `compaction_model` now records the model that ACTUALLY summarized (a change of
  value, not just a new key), alongside `compaction_model_configured`,
  `compaction_model_honoured`, and `compaction_model_method`. A fallback summary
  is recorded as `compaction_model: <fallback>` with
  `compaction_model_honoured: false` — the configured primary did not produce it.
- `_absorb_spend(gateway)` folds a secondary gateway's cost/tokens/calls into the
  run's own totals. A summarizer is real spend whichever model made it;
  `_absorb_fallback_spend` now delegates to it, so both tiers are accounted the
  same way. A test measures this: the run's total equals one charge per provider
  call, summarizer rows included.

### 3. `runtime.model_capabilities` is the ONE context-window authority

`budget_from_config` used to resolve a second window from
`context_window_tokens` / `context_window_by_model`. Two sources that can disagree
IS the bug. It now READS `resolve_context_window`. Two rules make that true:

- **The authority is a CEILING.** A config key may LOWER the window; it can never
  raise it past what the model really has, because raising a budget past the real
  window can only produce a request the provider rejects. So
  `context_window_by_model`'s documented purpose (stop a 32k model being run with
  a 128k budget) is now enforced rather than advisory.
- **The authority's `fallback` rung is a refusal to guess, not a capability
  answer**, so it never shrinks a run's declared budget. An unknown model keeps
  whatever the run declared. This matters: a hard ceiling on the fallback would
  have silently moved every unconfigured run from 32768 to 8192.

`ContextBudget` gains `window_source`, `window_model`, `window_declared`,
`window_authority`, surfaced in `as_dict()` with `window_authority_applied`.
`DEFAULT_CONTEXT_WINDOW` (32768) matches `DEFAULTS["context_window_tokens"]`, so
an unconfigured run is byte-identical to before.

**Measured, and worth knowing before you "fix" it:** the authority only *lowers*
where the model is known to be smaller than 32768 — `gpt-4` → 8192, `gpt-3.5` →
16385 in this tree. Every other resolution measured (`gpt-4o` 128000,
`qwen-*` 32768, `mistral`/`mixtral`/`stepfun` 32768, `deepseek-*` 64000,
`glm` 128000) leaves the effective window at 32768 or above, so the default run
is unchanged. A run with no `model` (every eval arm and every scripted stress
task) resolves the authority to `(0, "")` and keeps 32768 exactly.

The ladder is imported lazily and defensively: the harness must stay importable
with no runtime package present, and `litellm` costs ~30s to import on this host
unless the provider is one the harness implements itself
(`SYNTHETIC_PROVIDERS`). Pass `provider` through or a scripted run pays that
import.

### 4. The legacy step loop's cacheable prompt: BUILT, GATED, NOT ENABLED

`prompts.render_step_messages` was already implemented and pinned; Ceiling-09
recorded the conversion at `harness/core.py` as a blocked one-liner. It is now a
single config key, `"step_prompt_cache_split"` (default `None` — see §5), and
`run_step` takes the split when it is set. Either way `run_step` writes a
`step_prompt_cache` trace row with the prefix digest, breakpoint index,
prefix/suffix message counts, and prefix size.

`prompts.step_system_text(messages)` is the ONE dispatcher-safe reader. It
returns byte-identical text for BOTH shapes (it rejoins the split halves with no
separator, because the per-turn half always starts at `STEP_CACHE_BREAKPOINT`),
so a scripted model can be migrated before or after the split.

**It is not enabled, on purpose.** `next(m["content"] for m in messages if
m["role"] == "system")` — the exact expression used by the step loops' scripted
models — reads the FROZEN half under the split, and the frozen half does not
contain `your step is #N of`. Pinned by
`test_the_split_changes_what_a_first_system_message_dispatcher_can_read`, which
asserts the naive read stops matching and `step_system_text` still does.

**A second, larger finding, measured:** the legacy step prompt's frozen prefix is
the static template ALONE and measures **856 tokens**, below
`runtime.prompt_cache.DEFAULT_MIN_PREFIX_TOKENS` (1024). So even with the split
enabled, `plan_cache` reports `requested: false, skip_reason:
"prefix_below_floor"` and no breakpoint is sent. Enabling the split alone does
NOT buy a cache hit on this prompt shape; the prefix has to grow past the
provider's own floor (or the floor has to be lowered) for the conversion to pay.
Pinned by
`test_a_provider_that_reports_no_cache_usage_is_reported_as_unreported`, which
also shows the same prefix IS requestable once `min_prefix_tokens` allows it.
**Any hit-rate claim for this path is therefore unmeasured**, and the receipt is
REPORTED, never asserted: a cache hit rate is a property of the PROVIDER, and
`unreported` / `unsupported` are both non-decided states that must not be rounded
into a number.

### 5. Config keys

One new key: `step_prompt_cache_split`, default `None`. `None` and not `False`
because a value in `DEFAULTS` is merged into every task — a truthy default would
silently switch every run and every eval arm onto a different message shape, and
a half-migrated split looks exactly like a prompt regression. Absent / `None` /
`False` all mean the historical single interpolated system message. This is the
same pattern `declared_test_config_change` uses (R2-03) for the same reason.

No retry key is new: the kernel reads the existing `max_model_attempts`,
`model_retry_base_s`, `model_retry_cap_s`, so one knob governs both loops and a
run stays reproducible from its merged config.

### 6. Verification (real, this tree, `-p no:randomly`)

- **NEW `tests/test_ceiling_r2_08_backoff_context.py` -> 25 passed.** Named after
  the behaviour, not the implementation:
  `test_two_502s_produce_two_model_recovery_events_and_the_reply_returns`,
  `test_the_kernel_backoff_is_bounded_by_the_configured_attempt_budget`,
  `test_max_model_attempts_one_disables_the_retry_entirely`,
  `test_the_kernel_and_the_legacy_path_share_one_classifier_and_one_vocabulary`,
  `test_a_type_error_is_classified_model_internal_and_is_not_retried`,
  `test_a_provider_flavoured_message_on_a_harness_exception_is_still_internal`,
  `test_a_provider_flavoured_message_on_a_harness_exception_is_not_retried`,
  `test_the_receipt_names_the_summarizing_model_and_the_key_is_honoured`,
  `test_a_receipt_never_implies_an_unhonoured_compaction_model`,
  `test_the_summarizer_models_spend_is_folded_into_the_runs_own_totals`,
  `test_a_failed_primary_summarizer_falls_back_and_names_the_fallback`,
  `test_the_context_window_authority_is_the_only_resolver`,
  `test_the_authority_floor_never_shrinks_a_declared_budget`,
  `test_an_unconfigured_run_is_unchanged_by_the_authority`,
  `test_a_32k_window_model_is_never_issued_an_over_window_request[global_key]`
  /`[per_model_key]`, `test_a_config_pin_may_lower_the_window_but_never_raise_it`,
  `test_the_window_source_names_the_rung_that_produced_the_number`,
  `test_a_broken_authority_degrades_to_the_declared_window_rather_than_zero`,
  `test_the_cacheable_step_prompt_makes_the_leading_segment_byte_stable`,
  `test_step_system_text_reads_both_prompt_shapes_identically`,
  `test_the_step_prompt_cache_receipt_is_reported_and_never_gates`,
  `test_a_provider_that_reports_no_cache_usage_is_reported_as_unreported`,
  `test_the_step_prompt_split_is_off_unless_explicitly_requested`,
  `test_the_split_changes_what_a_first_system_message_dispatcher_can_read`.
  Both window tests measure the bound INSIDE the boundary, from the request it
  was handed, and assert the run used a meaningful part of the window it was
  given — so "never over-window" cannot pass vacuously.
- `tests/test_recovery_steering.py test_tool_protocol.py
  test_config_trace_state.py` -> **110 passed**.
- `tests/test_agent_loop.py test_prompt_cache_cost.py` -> **94 passed**.
- `tests/test_evals_run.py test_evals_tasks.py` -> **35 passed**.
- `tests/test_cli_runview.py test_modes.py` -> **157 passed, 2 skipped**.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0.
- `python -m evals.run --arms baseline,no_lint --tasks bug01_wrap --quick` ->
  **CLEAN**, exit 0, 2/2 arms ok, attempts=1, through the REAL loop and the REAL
  Docker verifier.
- `ruff check` clean on all six owned files plus the new test; `compileall` clean;
  scoped `git diff --check` clean (CRLF warnings only).

### 7. NOT run / red / honest

- **`tests/test_agent_kernel.py` -> 59 passed, 2 FAILED.**
  `tests/test_context_budget_engine.py` -> **8 passed, 10 FAILED.** Every one of
  the 12 is the SAME root cause and is **PROVEN not to be this round**: R2-06
  added `"require_edit_digest": True` to `harness/config.py` `DEFAULTS` at ~19:06
  on 2026-09-26 (it was previously OFF, per the Ceiling-04 notes), which refuses
  every scripted `write`/`edit` whose file the run never read — with
  `TOOL ERROR [stale_read] ... was never read in this run`. Those suites' growth
  fixtures write first and read after, so the context never grows, so compaction
  never fires, so the context assertions fail.
  **Controlled A/B, not an assertion:** a throwaway probe outside the repo
  (`Temp/opencode/probe_digest_gate_off.py`) that patches ONLY the registry's
  read of `require_edit_digest` to False turns `test_context_budget_engine.py`
  from 8/18 to **17/18** and turns
  `test_agent_kernel.py::test_safe_backend_overwrites_and_rolls_back_invalid_
  syntax` green, with no other change in the tree. The one A/B-unreachable case
  (`test_killed_run_resumes_with_its_context_budget_and_prior_turns`) is a real
  subprocess hard-kill test an in-process probe cannot patch; its child exits 0
  instead of 70 for the same reason.
  **I did not "fix" this by weakening an assertion or by reverting another
  terminal's `DEFAULTS` line.** `require_edit_digest` is not this prompt's key and
  `harness/editor.py` is not this prompt's file.
- **`python -m evals.run --quick` did NOT complete** inside a 30-minute window in
  this session. Not claimed, not counted. The cause was not established; the
  single-task real run above is clean.
- **No live-provider lane was run.** No credential was inspected, requested, or
  retained. Every provider-shaped failure in this suite is a constructed
  exception object (`BadGateway(status_code=502)`) and every model call is a
  scripted boundary. **Nothing here is evidence about any real provider's retry
  behaviour.**
- **No Docker lane beyond the one eval task above.**

### 8. Not implemented, stated plainly

- **The legacy step loop is not converted to the cacheable prompt shape.** The
  mechanism is built, gated behind one explicit key, and measured; the flip is
  blocked on a cross-terminal dispatcher migration (§9). It is not landed, and no
  cache hit rate is claimed for it.
- **`step_prompt_cache_split` has never been exercised through the full
  `harness.core.run_task` loop in a test.** The split path is covered at the
  `run_step` prompt-construction level and at the `prompts`/`prompt_cache` level;
  a full-loop arm with the key set would be the natural regression gate once the
  dispatchers are migrated.
- **The authority's ceiling is one-directional by design.** An operator cannot
  raise a window past what the authority resolved, even when they know better
  than a stale local table. `window_source` names the rung so a reader can see
  which number won; overriding the ceiling deliberately would need a new,
  explicitly-named escape hatch (and an INTERFACES entry), not a config value.
- **The kernel's recovery does not count toward a per-call cost of its own.** The
  `model_recovery` rows and `attempts` on the call record show the retries; the
  router's own spend is still the router's ledger, unchanged by this round.

### 9. Cross-terminal requests

1. **T01/T10 + eval owners — the step-prompt dispatcher migration, with measured
   evidence.** This is the one thing blocking the cacheable step prompt. Every
   caller below reads the FIRST system message and would silently lose
   `your step is #N of`, the issue text, the plan, and the pre-loaded files the
   moment `step_prompt_cache_split` is set. Replace the
   `next(m["content"] for m in messages if m["role"] == "system")` idiom with
   `harness.prompts.step_system_text(messages)`, which is correct for BOTH
   shapes, then set the key in a dedicated eval arm (not in `DEFAULTS`):
   - `tests/fake_model.py:55` (`ScriptedModel.__call__`)
   - `tests/resume_driver.py:62` and `:84`
   - `tests/test_webfetch.py:486, 550, 592, 640, 762` and the
     `users[-1].startswith("Begin.")` session-start checks at `:491, 555, 592, 640, 779`
   - `tests/test_batch_docs_lint.py:225, 292, 451, 506, 565, 690` and the
     `Begin.` checks at `:230, 297, 456, 511, 695`
   - `tests/test_build_plan_e2e.py:261, 503, 592`
   - `tests/test_e2e_run_task.py:1162, 1264`
   - `tests/test_steering.py:685`, `tests/test_agent_tests.py:259`,
     `tests/test_self_critique.py:67`, `tests/test_build_plan.py:593`,
     `tests/test_modes.py:1209, 1291`
   - `harness/_stubs/scripted_model.py:76` — **the cross-process hook** every
     eval worker and every `HARNESS_SCRIPTED_MODEL` subprocess resolves, so this
     one gates the whole matrix
   - `evals/run.py:126-133` (`_FeedbackAwareScriptedModel`) and
     `evals/daily_driver.py:4171, 4280, 4487`
   The `Begin.` checks are already safe (the opening instruction stays the final
   user turn in both shapes); the `system` reads are the ones that break.
   **Do the migration and the key in the same change**, or the matrix fails in a
   way that reads as a prompt regression rather than as a cache optimization.
2. **T03 (`runtime/model_capabilities.py`) — no change requested, one thing to
   know.** `harness/agent_kernel/budget.py` now depends on
   `resolve_context_window`'s returned `context_window_source` and treats the
   literal `"fallback"` rung as "no ceiling" (`harness/agent_kernel/budget.py`,
   `_resolve_window_authority`). If that rung is ever renamed, this reads it as a
   confident ceiling and would shrink budgets for unknown models. A named
   constant (e.g. `SOURCE_FALLBACK`) would remove that string coupling. I did
   not edit the file.
3. **R2-06 owner (`require_edit_digest`) — a real cross-terminal regression, not
   a request.** Flipping the default to `True` in `DEFAULTS` is correct policy
   and it breaks 12 currently-green tests in `test_agent_kernel.py` and
   `test_context_budget_engine.py` (§7, with the A/B). Those fixtures write
   before reading, so they now hit the strict gate. The fixtures need to read
   first and pass the digest, or pin `require_edit_digest: False` in their own
   config — **not** a change to the gate's default.
 4. **T11/shared:** unchanged from earlier rounds, still unfixed —
   `shared/security.py::redact_text` is quadratic in a single-character run.



## VEX-CEILING-02 — bounded context engine: tokens, compaction, meter, rewind (2026-09-26)

**A character cap is not a context budget, and compaction that cannot be undone
is context loss with a nicer name.** This round adds prompt-token measurement

per request, a category budget, compaction at a configurable FRACTION of the
window with a recorded fallback chain, an explicit `context_compacted` receipt,
reversible compaction metadata, constraint re-injection at the end of a long
context, a context meter on every surface, and an exact three-way rewind.

### 1. Token estimation (`harness/agent_kernel/budget.py`, NEW)

- `TokenEstimator` is **never-raising and dependency-free by default**.
  `heuristic` counts whitespace-delimited words at ~4 chars/token, which is
  close for code and prose and **rounds up** - the safe direction, because a run
  that over-estimates compacts a little early instead of overrunning the window.
  `auto` / `tiktoken` opt into `tiktoken` **only when its encoding is already
  cached locally** (`TIKTOKEN_CACHE_DIR` / `DATA_GYM_CACHE_DIR` contain the
  `cl100k_base` blob). A run must never block on a network fetch to size a
  prompt, so an uncached encoding degrades to the heuristic and
  `estimator.name` in the meter says which one actually ran. **Do not make
  tiktoken the default.**
- `ContextMeter` is the measurement: `used`, `limit` (the usable window),
  `threshold`, `utilization = used/limit`, `peak_utilization`, per-category
  tokens, estimator, compactions, dropped messages, re-injections, reclaimed
  tokens. `limit` is the window (not the trigger), so
  `utilization_p95 <= 0.80` is computed by `evals/slos.py::_context_utilization`
  from the same `context_budget` rows this module writes - no second convention.
- Categories: `system_rules`, `user_messages`, `assistant`, `tool_results`,
  `retrieved_files`, `memory`, `diffs`, `constraints`, `other`. A content
  marker beats the role, because the kernel deliberately packs diffs, memory,
  retrieved files, and the re-injection into user messages.
- `plan_drop` returns the **oldest droppable** messages only. The base
  `[system, user]` frame, the structured handoff, and the trailing N messages
  (default: the newest tool result) are never droppable.
- `budget_from_config` reads `context_window_tokens`,
  `context_window_by_model` (so a 32k model is not run with a 128k budget),
  `context_reserved_output_tokens`, `context_compaction_fraction`,
  `context_token_estimator`, `context_category_shares`, `context_budget_enabled`.

### 2. Compaction (`conversation.compact_tokens` + `strategy._compact_context`)

- Fires when `measure()` reaches `threshold = usable_window * fraction`
  (default 0.6), i.e. **before** the window is at risk, never at its edge.
- The summarizer is ONE model call with **no tool schemas**, on a prompt from
  `harness/prompts.render_compaction_prompt` (bounded again by
  `context_compaction_input_tokens`, so a compaction cannot itself build an
  over-window request).
- **The fallback chain is explicit and recorded**: `model_summary` ->
  `fallback_model` (a second `ModelGateway` bound to
  `context_compaction_fallback_model`) -> `structural_trim` (no summary; the
  deterministic structured handoff still carries the facts). The method that
  ran is on the receipt and in the `context_compacted` event. A missing
  fallback model emits `context_compaction_fallback_unavailable` - it is never
  assumed.
- **Two things the fallback needed and would silently get wrong without them**:
  the second gateway reuses the run's own boundary via
  `ModelGateway.call_fn` / `.model_client` (a fallback that resolved the
  default provider instead would dial a real one in a scripted run), and its
  spend is folded back into the run's totals by `_absorb_fallback_spend()`
  (a summarizer call is real spend; a second ledger would under-report cost).
- `ModelGateway._invoke` now forwards `step` to the boundary. `_invoke_callable`
  drops it for every boundary that does not declare it (the real router does
  not), and it is what makes a maintenance call distinguishable from a turn.
- What is never droppable: the base frame, the structured handoff, the newest
  tool result, the constraint re-injection. A compaction cannot eat the rules
  the run is bound by.

### 3. Reversibility (the part that makes it a budget and not a leak)

- Every retained conversation turn is mirrored into
  `logs/{run_id}/conversation.jsonl` through `ConversationMemory`'s `journal`
  sink: `base`, `turn` (full content + `seq` + `turn`), `compaction`, and
  `restore` rows. **The memory object itself stays bounded** - the journal, not
  the object, is the reversibility mechanism - so a 5,000-turn session costs
  the same memory as a 5-turn one.
- A character/message fold is journaled as a **compaction row too**
  (`method: "budget_fold"`). Otherwise the deterministic part of the handoff
  would be unreconstructable.
- `ConversationJournal.live_snapshot()` is a deterministic fold: base + every
  turn row whose `seq` is not in the dropped set of an ACTIVE compaction, plus
  the handoff of the last active compaction. `ConversationMemory.restore()`
  rebuilds the in-memory list from it, so a restored run renders byte-identical
  requests.
- `ConversationJournal.restore_compaction(id)` appends a `restore` row, which
  **deactivates** one compaction and un-drops exactly its sequences. History is
  never rewritten; rolling back twice honestly reports nothing to restore.
- `strategy.restore_compaction(id)` is the in-process form and updates the
  meter.
- `logs/{run_id}/compactions.jsonl` holds one receipt per compaction
  (`dropped_seqs`, method, tokens before/after, what survived, the restore
  procedure). `logs/{run_id}/context.json` is the atomically rewritten meter
  artifact. **Both are redacted** (`shared.security.redact_secrets`) because the
  journal holds full tool output.

### 4. Constraint re-injection

`prompts.render_context_reinjection` produces a block marked `[neo-reminder]`
(the marker is what lets the classifier count it as `constraints` rather than
user text). It is rendered per request and appended AFTER the history by
`ConversationMemory.render(extra_messages=...)`, so it rides the end of a long
context without accumulating one copy per turn. Trigger: turn >= 2 AND
`utilization >= context_reinjection_fraction` (default 0.35). It restates the
request, the turn, the changed files, the protected paths, the honest
verification rule, and the current fill level.

### 5. Three-way rewind (`harness.agent_kernel.context.rewind_run`)

One function, two independent axes, both exact - never an approximation:

| scope | authority | touches |
|---|---|---|
| `conversation` | `conversation.jsonl` + `turns.jsonl` + `checkpoint.json` | no repository file |
| `files` | per-turn pre-image store + the run's `pristine/` snapshot | no conversation record |
| `both` | both | both |

- `runtime.checkpoint.TurnFileState` captures the bytes of the files a run has
  CHANGED at the start of each turn. `pre_image(rel, turn)` is an **exact-turn**
  lookup, and that is what makes it exact: an image exists for a turn only if
  the file was already tracked at the START of that turn, so the image IS the
  state that turn saw. A file that was still pristine then is restored from the
  pristine source - using a later turn's image would restore a moment the rewind
  never asked for. A file the run CREATED is removed (`exists: false` image, or
  absent from pristine).
- Rewinding truncates append-only journals and **rotates the previous file
  aside** (`.rewound-<ns>`), never deletes it. `rewind_checkpoint` re-derives
  the event sequence, agent-owned changes, and spend from the surviving rows and
  **drops the resume token** - the turn state changed underneath it, so a
  relaunch must re-authorize.
- `DailyCodingStrategy.rewind(turn, scope)` delegates to `rewind_run` and then
  restores the in-memory conversation and re-syncs the changed-file set, so a
  live rewind and an offline picker rewind cannot diverge.
- The picker reads `cli.runview.read_rewind_targets(log_dir)` (one row per
  `turns.jsonl` record, newest first, with the tools and files of that turn).

### 6. Surfaces

- `context_budget` and `context_compacted` are journal rows emitted on **every
  prepared request**; `context_compacted` also carries `survived` and
  `reversible`. `shared/traceview.py` retains and renders them, so
  `python -m shared.traceview <task_id>` shows compaction in the timeline.
- `RunResult.metadata["context"]` carries the whole meter (so `--json` needs no
  journal read), and `cli/runview.read_context_meter(log_dir)` reconstructs it
  from `context.json` + the journal for any run, including one whose artifact is
  missing. `cli/runview.context_meter_line()` renders it; `card_lines` and
  `status_lines` (live TUI/REPL) show it, and `RunProjection` folds the journal
  rows so the live view needs no extra file read.

### 7. Config keys (all in `harness/config.py`, all additive)

`context_budget_enabled`, `context_window_tokens`, `context_window_by_model`,
`context_reserved_output_tokens`, `context_compaction_fraction`,
`context_token_estimator`, `context_category_shares`,
`context_compaction_model`, `context_compaction_fallback_model`,
`context_compaction_input_tokens`, `context_reinjection_enabled`,
`context_reinjection_fraction`, `context_rewind_max_files`.

### 8. Verification (real, this tree, `-p no:randomly`)

- **NEW `tests/test_context_budget_engine.py` -> 18 passed.** The eight required
  proofs, each against the REAL kernel: a 5,000-message session has flat
  retained messages, flat measured tokens, and a flat serialized snapshot
  (last 8 samples identical); a 32k-window model never receives an over-window
  request (the guard lives INSIDE the scripted model and measures the request it
  was handed); compaction fires below the fraction and the run completes with
  `utilization_p95 <= 0.8`; a summarizer failure falls back to the cheaper model
  and the receipt, the `compactions.jsonl` row, AND the next model request all
  name it; a no-fallback run is recorded as `structural_trim` with the
  deterministic handoff intact; a real `os._exit(70)` mid-run resumes with its
  budget, its receipts, and its prior turns; conversation-only rewind leaves the
  tree byte-identical AND `git status` identical; files-only rewind leaves
  `conversation.jsonl` / `turns.jsonl` / `checkpoint.json` byte-identical; a
  both-rewind equals an INDEPENDENT oracle (a run interrupted by a real
  `KeyboardInterrupt` at the same turn) on files, git status, the conversation
  journal, and the turn ledger. Plus the budget/estimator/pre-image/picker unit
  contracts and compaction visibility in `traceview`.
- `python -m pytest tests/test_context_budget_engine.py tests/test_agent_kernel.py
  tests/test_agent_loop.py tests/test_config_trace_state.py -q -p no:randomly`
  -> **141 passed**.
- CLI/tracing regression: `test_cli_runview test_cli_tui test_cli_session
  test_cli_tracelog test_tracing test_ceiling15_spans test_slos` -> **315
  passed**.
- Harness/evals regression: `test_scheduler_integration test_daily_driver_evals
  test_evals_run test_evals_tasks test_stubs_and_deps test_modes` -> **218
  passed**.
- `python -m evals.run --suite prompt-regression --check` -> **14/14 CLEAN**
  (no prompt regression from the new compaction/re-injection prompts).
- `python -m evals.run --suite daily-driver --no-docker --json` -> **46/52 arms
  pass, verdict `ERROR`, `ready: false`** - reported honestly, NOT as a pass.
  The three failing pairs (`dd_02_dirty_small_edit`, `dd_04_interpret_test_failure`,
  `dd_19_skill_discover_show_inject`) are the SAME three, on the SAME legacy
  `harness/agent_loop.py` path, that terminal 04 already recorded as failing
  before this round. Their only failing assertion is
  `run_completed: result.get("status") == "success"`; every content assertion is
  `true`. Terminal 01's honest-completion change made that expectation
  unsatisfiable without re-asserting the lie this project forbids. **T10 owns
  the fix** (the probe must accept a completed-but-unverified legacy run); this
  round did not weaken the invariant to make the probe pass.
- `ruff check` clean on every owned/edited file; `ruff format --check` clean on
  the new files; `compileall` clean; scoped `git diff --check` clean.
  `graphify update .` -> **30,722 nodes / 147,521 edges / 4,015 communities**
  (HTML viz skipped by the tool's own size guard).
- **No Docker lane and no live-provider lane were run.** They are NOT selected
  and are NOT reported as passes. The hard-kill proof is a real subprocess
  `os._exit(70)`; every model call in this suite is a deterministic scripted
  double, so nothing here is model-quality or real-sandbox evidence.

### 9. Not yet implemented / honest notes

- **Token estimation is a calibrated heuristic by default, not a tokenizer.**
  With `tiktoken` unavailable offline that is the only honest option; the
  meter's `estimator` field always says which estimator ran, so a measurement is
  never presented as exact when it is approximate.
- **Reversibility is bounded by what the journal stores.** The conversation
  journal holds the full bounded content of each retained turn, so a restore is
  exact - but that is a disk cost per turn. There is no retention policy for
  `conversation.jsonl` yet (`shared/retention.py` exists and is not wired here).
- **Rewind restores files the run changed and the pristine snapshot covers.**
  Files mutated outside the run's tracked set are not restored, and the receipt
  says so per path rather than claiming a full restore.
- The rewind **UI** is data-only: `read_rewind_targets` feeds a picker, and
  `rewind_run` performs the action. No `/rewind` slash command or TUI screen was
  added - those surfaces belong to terminal 03's shell work.
- `context_compaction_model` is read into the receipt but a dedicated primary
  summarizer model is not yet honoured: the summarizer uses the run's own model
  unless a fallback is configured. The key exists so the receipt can name what
  would be used.

### 10. Cross-terminal notes and requests

- **T11/shared (`shared/security.py`): a real quadratic defect, reported, not
  fixed here.** `redact_text` takes time quadratic in the length of a
  single-character run: `n=2000` 0.05s, `n=4000` 0.19s, `n=8000` 0.86s,
  `n=12000` 3.0s, `n=16000` 6.5s, and a 40k run (one `write` of `"y"*40000`)
  hangs the journal for minutes. Mixed text is fine (`n=40000` 0.03s), so it is
  the repeated-character class, not the length. Reproducer:
  `for n in (2000,4000,8000,12000,16000): time redact_text("y"*n)`. This blocks
  any large single-class payload - a minified asset, a base64 blob, a padding
  file - and it is on the path of every journal write, so it is worth a bounded
  pre-scan (cap the scanned span per line, or a linear-time character-class
  check before the pattern pass). I did not edit that file.
- **T10 (`evals/daily_driver.py`):** the three legacy-path `run_completed`
  expectations above. Accept a completed-but-unverified legacy run (and keep
  `no_false_verified_claim` as the real guard).
- **T05 (context compiler / LSP), who is editing `strategy.py` concurrently:**
  two shared-file hazards found and handled, both worth knowing.
  1. `ContextBuilder.build` has a `for turn in state.turns[-8:]` loop whose
     variable is named `turn`. My budget parameter is `turn_index` for exactly
     this reason - naming it `turn` shadowed the loop variable and turned every
     bundle into a `TypeError`. Do not reintroduce a `turn` parameter there.
  2. `strategy.py` was being written concurrently (a `return ""` and a
     `targets: List[str] = []` landed on one line at 01:09, breaking the import
     for ~90s). It parsed again on its own; no repair was made and no in-flight
     hunk was reverted. The kernel suites were re-run green after their write
     landed.
- **T04 (`harness/agent_kernel/gateway.py`):** two additive changes were needed
  and are called out above - the `call_fn` property and forwarding `step` to the
  boundary. Both are keyword-filtered, so every existing boundary and test double
  is unaffected; please keep them when the catalog work lands.
- **T01 (`harness/agent_kernel/conversation.py`):** this module was created for
  this round's budget work and the additions are strictly additive
  (`journal` sink, `snapshot`/`restore`, `compact_tokens`, `summaries` on the
  handoff, `to_record`/`from_record`, `render(extra_messages=...)`). The only
  behavior change to existing code is that a character/message fold now emits a
  journal row, which is what makes it reversible.

## VEX-CEILING-07 — recovery is a policy, steering reaches the tool boundary (2026-09-26)

**Classifying an error and then telling the model the same sentence is
presentation. This round makes the classification change what the loop DOES.**
Everything new lives in `harness/tool_errors.py` (the policy engine) and
`harness/steering.py` (the in-flight abort watcher); the loops in
`harness/core.py` and `harness/agent_loop.py` are the call sites. Closes gap
matrix G13 (recovery), G14 (interrupt), G15 (model failure), G16 (output).

### 1. `harness.tool_errors.POLICY` — the table IS the policy

Every error kind maps to an **action** (what the loop does) plus an
instruction (what the model is told). A table where two kinds share an action
is not a policy, so `test_every_policy_kind_has_a_distinct_action` pins the
distinctness.

| kind | action | what actually changes |
|---|---|---|
| `timeout` | `narrow_and_extend` | a strictly narrower next command AND a larger bounded per-command budget |
| `file_not_found` | `attach_listing` | a real bounded directory listing of the parent dir |
| `permission_denied` | `forbid_path` | the path is added to a do-not-retry set, and a command whose EVERY path token is forbidden is **refused before it runs** |
| `malformed_patch` | `attach_numbered_file` | the exact current file with line numbers |
| `malformed_tool_call` | `restate_schema` | the schema restated plus **one worked example** |
| `model_unavailable` | `bounded_fallback` | the named fallback tier; never a dead run |
| `verification_failed` | `preserve_and_replan` | the verifier evidence preserved verbatim + `replan=True` |
| `loop_detected` | `stop_and_ask` | the turn is stopped and the call is not dispatched |
| `command_not_found` | `avoid_binary` | the binary is added to the do-not-retry set |
| `command_rejected` | `avoid_shape` | the deny-guard shape is recorded as a no-op to repeat |
| `internal_error` | `inspect_output` | the baseline hint |

`RecoveryPolicy` is **one instance per task** (not per attempt, not per step),
which is what makes the forbidden-path set, the escalated timeout, the loop
guard and the statistic span the run. `stats()` reports per-kind occurrences,
recoveries, a `pending` flag, and `mean_turns_to_recovery`. An error still
pending at report time is **not** counted as recovered — an unrecovered failure
must never read as a fast recovery.

**`narrower_command` is deliberately conservative.** It only rewrites commands
whose scope it can bound with certainty (pytest family → `-x` and/or
`--tb=short`; `find` → `-maxdepth`; `grep`/`rg` → a single file-type filter)
and returns `None` otherwise, so the loop falls back to "run a smaller slice"
rather than corrupting a command it does not understand. Two subtleties that
were real defects first: `-q` is NOT treated as bounding traceback length (only
`-v`/`--tb` are), and `--maxfail=2` suppresses `-x` (they conflict) while
still narrowing the traceback.

**`malformed_patch` never presents the patch file as the target.**
`_patch_target()` filters `.patch`/`.diff`/`.rej` and shell pseudo-paths
(`/dev/stdin`, …), prefers a token that is a *readable* file, then falls back
to the session's own `files_touched`, and only then to an honest
"no target file could be identified". Attaching the numbered contents of a
file that does not exist would be evidence invented for the model's benefit.

### 2. Model failure tolerance (G15)

`ModelRecovery.call(fn)` retries a model call with **bounded** exponential
backoff (`base * 2^(n-1)`, capped; deterministic, so it is test-assertable)
and emits one `model_recovery` event per decision with `action: retry|gain_up`.
Classes: `model_rate_limited` (429), `model_unavailable` (5xx/connection/
overload), `model_timeout` are **retryable**; `model_auth` (401/403),
`model_bad_request` (400/404) and `model_internal` are **terminal**.

Two decisions worth knowing:
- An **unrecognised PROVIDER** failure defaults to `model_unavailable`
  (retryable): a bounded retry beats killing a healthy run, and the bounded
  budget is what makes the assumption safe.
- A **non-provider** exception is always `model_internal` and is never retried,
  however provider-flavoured its message. `TypeError("unsupported operand for
  timeout_ms")` is our bug, not a provider outage; retrying it three times is
  noise, and labelling it `model_timeout` would blame the provider for a
  coding error on this side of the boundary.

`FATAL:` in `run_step` is now reserved for **terminal** failures only and
carries the kind and the attempt count. A 502 blip can no longer end a run.

### 3. Steering reaches the tool boundary (G14)

Before, steering was only observable *between* model calls, so a `sleep 300`
already dispatched to the sandbox could not be interrupted — the loop did not
look again until the command returned, up to `command_timeout_s` later.

- `BashSession` takes an optional `cancellation_token` and gained `cancel()`,
  the `cancelled` property, and `set_timeout()` (which can only **widen** the
  budget). The token is forwarded to the sandbox **only when the resolved
  callable's signature accepts it**, so every three-positional-arg Boundary-1
  sandbox and every test double keeps working byte-identically.
- `HardAbortWatcher` polls the SAME steering journal (50ms) while a command
  runs and calls a **zero-argument** kill hook. It never consumes the event —
  the loop's single consume point still owns it, so a resumed run cannot lose
  the abort — and it records a failing hook in `errors` (surfaced as
  `steering_abort_watcher_error`) rather than letting an interrupt fail
  silently. **The zero-arg contract is pinned by a test because the first
  version passed a reason and silently failed to interrupt anything**; only the
  end-to-end in-flight test caught it.
- An observed abort yields `steering_abort_in_flight` and the command's
  output is appended to the conversation **before** the step ends, with a note
  that says the result was preserved. The loop exits are unchanged, so `work/`,
  `state.json` and `plan.json` survive and the run stays resumable.

Three checks now run **before** a command is dispatched, and each one can
change what happens: a pending hard abort (yield), a forbidden-path refusal
(`command_refused`, command not run), and the repeated-command guard
(`loop_guard`, step note `LOOP-GUARD: ...`).

### 4. Loop protection — and the read-only exemption that is load-bearing

`note_command` fingerprints a command as whitespace-normalized text and stops
the turn after `recovery_max_repeat_command` identical observations; the
refused call is never dispatched.

**Read-only commands are exempt by default** (`recovery_loop_guard_read_only:
False`). This is not a nicety. The first version guarded every command and the
**real Docker e2e lane caught the regression**:
`test_verified_success_state_complete_after_exhausted_turns` scripts three
identical `cat` calls and expects the exhausted-turns path; the guard fired
and the step ended `LOOP-GUARD` instead. A model re-reading a file after its
own edit is legitimate, so the guard now draws the same line the typed tool
catalog's own `loop_guard_read_only` does. `is_read_only_command` is
conservative in **both** directions — a known read verb, no recognised mutating
shape, and no shell composition at all outside quotes (`cat a.py > b.py` starts
with a read verb and ends in a write). A full `pytest` run is **not**
read-only: it writes caches and runs arbitrary test code, and repeating one is
exactly the doom loop this exists to stop.

### 5. Output shaping (G16)

`shape_tool_output(text, limit, *, head_ratio=None)` is head+tail with an
explicit `[... N chars omitted ...]` marker, always. For **pytest-shaped**
output the split biases to 15% head / 85% tail, because the answer (the last
failure's traceback and the short test summary) is at the END and a 50/50 split
spends half the budget on the collection preamble.

`is_pytest_shaped` samples **head and tail**, which is load-bearing: a long
pytest run opens with a collection log containing no marker at all, so a
head-only check classifies the very output this exists for as unrecognised and
silently falls back to the even split. That was a real defect, caught by
`test_pytest_detection_samples_the_tail_not_just_the_head`.

`harness.tools.truncate` is **unchanged** (it is test-pinned and callers depend
on its exact shape); `BashSession` shapes through `shape_tool_output`.

### 6. Config keys (all additive, all defaulted)

`recovery_timeout_backoff` (1.5), `recovery_max_timeout_s` (600),
`recovery_max_repeat_command` (2), `recovery_loop_guard_read_only` (False),
`recovery_listing_limit` (40), `recovery_numbered_file_lines` (160),
`recovery_numbered_file_chars` (6000), `recovery_fallback_model` (None),
`max_model_attempts` (3), `model_retry_base_s` (0.5), `model_retry_cap_s` (8.0),
`abort_kill_deadline_s` (5). No recovery constant is hardcoded in a loop, so a
run stays reproducible from its merged config (spec item 10).

### 7. What this round did NOT change, deliberately

No verifier, no `CompletionPolicy`, no completion status, no `state.json` key,
no Boundary 1/2/3 signature. The success mint is still keyed on
`final_v.target_test_passed and final_v.regression_passed and not final_v.flaky`
and the recovery machinery appears **nowhere** in that condition
(`test_the_verifier_contract_is_untouched_by_recovery` pins this by reading the
source). `harness/tools.py` was edited only additively (the optional token,
`last_error`, and the output-shaping delegation) because it is Terminal 04's
file; `harness/config.py` only got defaulted keys because it is Terminal 01's.
`harness/agent_kernel/strategy.py` — Terminal 01's — was **not** touched, so
the kernel's own `model_recovery` path and Terminal 04's `ToolLoopGuard` are
unchanged; the bash-path guard here is complementary, not a second orchestrator.

### 8. Verification (real, this tree)

- **NEW `tests/test_recovery_steering.py`: 67 passed.** All six required tests
  are named after the prompt and assert behaviour, not strings. Measured, not
  asserted-from-a-comment:
  - `sleep 300` aborted in **0.39s** (real Docker container kill, exit 130) and
    **0.86s** (real host process-tree kill) — both against the 5s bound;
  - two 502s recovered with two `model_recovery` retry events and the reply
    returned, both at the helper level and through the real `run_step`;
  - a timeout produced `python -m pytest tests -q -x --tb=short` with the
    budget raised 120→180→270…→capped 600;
  - a 20k failing pytest output kept its assertion **because** of the tail bias —
    the test asserts the even split DROPS it, so it discriminates;
  - repeated identical calls stopped the step with the third never dispatched;
  - steering reached the next boundary with one inject / one consume and every
    journal line parsing.
- `tests/test_e2e_run_task.py` (real Docker sandbox + verifier) → **28 passed**.
  The first run was 27 passed / 1 failed; that failure was the read-only
  regression described in §4, fixed and re-run green.
- `tests/test_recovery_steering.py test_tool_errors.py test_steering.py
  test_tool_protocol.py test_retrieval_tools.py test_adversarial.py
  test_batch_docs_lint.py test_workspace_security.py` → **309 passed, 12
  skipped** (skips are Docker-gated, i.e. BLOCKED, not passes).
- `tests/test_agent_kernel.py test_agent_loop.py test_config_trace_state.py`
  → **123 passed** — Terminal 01's own required suite, unchanged.
- `test_editor_prompts.py test_daily_driver_evals.py` + the same kernel
  selection → **179 passed, 1 skipped**.
- `test_verify.py test_workspace_security.py test_scheduler_integration.py
  test_skills.py` → **143 passed, 12 skipped**.
- `python -m evals.run --check` → **14/14 CLEAN**.
  `python -m evals.run --quick` → **40/40, verdict CLEAN, 0 regressions** (5
  tasks x 8 arms through the real loop).
- `ruff check` + `compileall` clean on all owned/edited files; scoped
  `git diff --check` clean.

### 9. Not run / honest blocked status

- **No live model/provider lane.** No credential was inspected, requested, or
  retained. Every provider failure in the suite is a constructed exception
  object with a realistic class name and `status_code`; every model call in the
  loop tests is the in-repo scripted fake. Nothing here is evidence about any
  real provider's retry behaviour.
- **No Windows PTY lane** (unavailable with the current dependency set),
  unchanged and unrelated to this round.
- `harness/agent_kernel/strategy.py` (the kernel's model-call path) still has
  no bounded backoff. It is Terminal 01's file and its own `model_recovery`
  event shape differs; the integration request is in
  `logs/ceiling/terminal-07.json`.

## VEX-CEILING-04 — native tool protocol and one canonical catalog (2026-09-26)

**There is now exactly ONE tool list in this repository:
`harness.tools.typed_tool_specs()`.** `harness/agent_kernel/tools.py` no
longer declares tools; `builtin_tool_specs()` derives its kernel `ToolSpec`
objects from the production catalog on every call, and
`ToolRegistry.schemas()` returns the canonical
`harness.tools.typed_tool_schemas()` filtered to the registry. Parity is
machine-checkable: `harness.tools.catalog_parity_report(derived)` /
`harness.agent_kernel.tools.catalog_parity()` report any missing, extra, or
divergent entry and a SHA-256 `catalog_fingerprint` over names, required and
optional arguments, argument types, aliases, and effect classes.

### What changed in the catalog

- Canonical catalog is **38 tools** (was 20 in the kernel list). Added to the
  single catalog so the kernel has no private entries: **`verify`** and
  **`cancel`**. `test` now accepts both `command` (the kernel's verifier-gated
  handler reads it) and `test_command` (the production
  `execution.verify` boundary's documented name).
- **The input-request tool's canonical name is now `ask`, with `question` as
  its alias.** `harness.agent_kernel.strategy` matches the literal `"ask"` when
  it turns a call into `needs_input`, and `execution.workspace` already maps
  `ask -> question` in `policy_for_tool` / `SafeToolBackend.execute`, so
  `ask` is the name both the kernel and the backend accept. Consumers must
  resolve through the alias index (`typed_tool_spec`, `ToolRegistry.spec`,
  `catalog_parity_report`) instead of assuming `question` is canonical.
  `runtime/roles.py` was made alias-aware (2 blocks) because it validated
  role `visible_tools` against canonical names only and would otherwise fail
  at import time; `tests/test_workspace_security.py` was updated to assert the
  alias resolves to the same spec.

### Native provider tool calling (where the schemas go)

`ModelGateway -> ModelClient/runtime.call_model -> litellm.completion`. The
catalog's provider-neutral schemas are forwarded **verbatim**; `ModelGateway`
passes only the arguments the boundary's signature accepts, so the pre
tool-protocol stub router and test doubles keep working untouched.

- `runtime.model_router.call_model(..., tools=None, tool_choice=None)`. With
  `tools` supplied and the provider returning native calls, the router returns
  `{"content", "text", "tool_calls", "finish_reason"}`. **Without native calls
  it still returns the historical `str`**, so `ModelClient`, the difficulty
  estimator, and every other Boundary-2 consumer are byte-identical.
- `ModelResponse` gained `tool_protocol` (`native` | `text` | `none`),
  `stop_reason`, `protocol_errors`, and `duplicate_events`. The `model_response`
  trace record now carries the protocol actually used, how many schemas were
  sent, the call count, and the stop reason — **a text-protocol fallback is
  recorded as a fallback and never presented as native.** The receipt is
  written after parsing (`_note_protocol`) so the trace cannot claim a native
  call the provider never made.

### Validation before persistence

`parse_model_response` JSON-projects every argument (`json_safe`), so an
assistant turn can never wedge `trace.jsonl` on reload. `ToolRegistry.dispatch`
turns each schema violation into a **tool result** (stable `error_kind`:
`validation_error`, `stale_read`, `ambiguous_match`, `no_match`,
`loop_detected`, `no_runtime`, `handler_error`) instead of an exception, so one
bad call in a batch never costs the session its remaining valid calls.
`dedupe_tool_calls` / `ToolRegistry.note_event` drop retried or duplicated
provider events by event id, so a duplicated stream frame executes a tool
exactly once. `_infer_stop_reason` records `incomplete_truncated`,
`incomplete_refusal`, or `empty_response` instead of ending a turn silently.

### Safe edits — and the honest compatibility decision

- `read` appends a machine-parseable trailer
  `[neo-file-digest] path=<p> sha256=<hex> chars=<n>` and sets
  `ToolResult.digest`; the digest is passed back as `expected_revision`.
- A supplied digest that no longer matches is refused **before dispatch** with
  the `stale_read` slug. A multi-match `edit` is refused as `ambiguous_match`;
  a missing one as `no_match`. Both are protocol failures, not task failures.
- **A digest the model omits is bound from the session's own earlier
  observation** — the digest recorded by `read`, else the first time the
  registry had to observe the file — and is re-baselined after each mutation
  this run itself applies. It is therefore never a hash taken at dispatch
  time. `require_edit_digest: True` (config) makes a mutation of a file the
  session never read a hard `stale_read` refusal.
- **Measured deviation, stated plainly:** the catalog keeps
  `expected_revision` REQUIRED (Terminal 02's `validate_typed_arguments`
  fail-closed contract and its test are unchanged), and the kernel satisfies
  that requirement by binding. Defaulting instead to "refuse every edit that
  did not quote a digest" would have broken the currently-green 52-arm
  daily-driver matrix and the T1-owned scripted strategies, which issue edits
  without a prior `read`. Flipping to the strict reading is the single config
  key `require_edit_digest`.
- A file that cannot be read at all (protected path, symlink, vanished) is
  **not** reported as `no_match` or `stale_read`: the mutation handler and the
  permission policy own that refusal. This was a real defect the
  `dd_21_repair_broken_test` arm caught.

### Loop protection

`ToolLoopGuard` fingerprints a call as canonical name + JSON-normalized
arguments. After `max_repeat_tool_calls` identical observations (default 2)
the call is refused with `loop_detected` and **is not dispatched again**.
Read-only tools are exempt by default (`loop_guard_read_only`). The refusal is
a model-facing result, so the strategy's existing `tool_recovery` path nudges
the model and its `max_tool_recoveries` cap ends a real doom loop.
`failure_report()` keeps `protocol_failures` and `task_failures` separate.

### Kernel handlers for the whole catalog

`ToolRegistry` stays **handler-free on construction** (so
`runtime.roles.build_role_registry`'s "handler-free registry" contract holds)
and a strategy still wins with `set_handler`. A catalogued tool with no
installed handler is dispatched through the run's **safe execution backend**
(`context["execution_backend"]`) — the daily strategy builds it, so `delete`,
`rename`, `undo`, `lint`, `typecheck`, `build`, `list`, `image`, the six
`git_*` reads, the three `process_*` controls, `web_search`, and `task` are
all executable. `web_fetch` uses `harness.webfetch`; control tools journal a
`control_intent` event. **With no backend bound the call is refused honestly**
(`no_runtime`) — never executed through an untyped fallback. `task` is a
journaled intent record and deliberately **not** a spawn: recursive model
spawning stays owned by `runtime.orchestration`.

### Verification actually run

- `python -m pytest tests/test_tool_protocol.py -q -p no:randomly` -> **29
  passed** (new suite: native schema reaches a fake provider, router forwards
  schemas to a fake litellm and returns normalized calls, the router keeps
  returning a string without native calls, text fallback is explicit in the
  trace, a pre-tool boundary still works, malformed JSON does not wedge,
  validation errors are results, calls are JSON-serializable, a duplicated
  event executes once, gateway cross-turn dedupe, stop-reason inference,
  protocol vs task failure counters, catalog parity + divergence detection,
  alias-aware restrict, every catalog tool has a handler, read digest,
  stale edit rejected, digest binding, strict digest policy, ambiguous and
  missing matches, and loop protection).
- `python -m pytest tests/test_tool_protocol.py tests/test_agent_kernel.py
  tests/test_workspace_security.py tests/test_model_router.py
  tests/test_orchestration.py -q -p no:randomly` -> **178 passed, 1 failed**.
  The one failure is **pre-existing and unrelated**:
  `tests/test_model_router.py::TestFailureLedger::test_ledger_write_failure_is_not_silent`
  (it chmods a directory read-only and expects an `OSError`, which Windows
  does not raise; it failed identically before this round).
- `python -m pytest tests/test_agent_loop.py -q -p no:randomly` -> **62
  passed**. `python -m pytest tests/test_daily_driver_evals.py -q
  -p no:randomly` -> **34 passed**.
- `python -m evals.daily_driver --case dd_21_repair_broken_test --arm
  adversarial` -> `completed_verified` after the two real defects above were
  fixed (it is the arm that caught them).
- `python -m evals.run --suite daily-driver --no-docker --json` -> **46/52
  arms pass**. The 3 failing case pairs (`dd_02_dirty_small_edit`,
  `dd_04_interpret_test_failure`, `dd_19_skill_discover_show_inject`) run the
  LEGACY `harness/agent_loop.py` path and are **pre-existing in this shared
  tree, not caused by this round**: re-running them with only
  `harness/model_client.py` reverted to HEAD produced byte-identical results
  (every content assertion true, only the legacy `run_completed` status
  expectation false). Verdict `ERROR`/`ready=false` is reported honestly, not
  as a pass.
- `python -m ruff check` and `python -m ruff format` pass for all owned files;
  `python -m compileall -q` passes; scoped `git diff --check` passes.
  `runtime/roles.py` keeps its pre-existing whole-file format debt (it was
  already untracked-and-dirty from the Terminal 08 round); only the lines this
  round added were formatted.
- **No live-provider lane was run and no credential was inspected.** The
  provider, Docker-verifier, and manual-repair lanes are **BLOCKED / NOT
  SELECTED**, not passes.

### Not yet implemented / honest notes

- `require_edit_digest` defaults **off** (see the measured deviation above).
- `web_search` requires the safe execution backend; there is no bounded host
  search fallback, so a run without a backend gets an honest `no_runtime`.
- The kernel's `read` handler body lives in `harness/agent_kernel/strategy.py`
  (Terminal 01's file, not this round's ownership); the digest trailer and
  digest recording are applied by `ToolRegistry` after that handler returns,
  so the contract holds for every `read` dispatch path without editing it.
- A Windows PTY, a real live provider, and the Docker verifier lane were not
  exercised by this round.

## VEX-CEILING-01 — one default agent path (2026-09-26)

**Terminal 01: the verified kernel is now the one authoritative run path, and
the daily strategy keeps a real conversation instead of rebuilding a fresh
`[system, user]` prompt every turn.** Five pre-existing test failures found in
the live tree are fixed here; they were caused by terminal 04's canonical tool
catalog rewrite landing between handoffs, not by this round.

### 1. One authoritative run path

- `resolve_agent_strategy(config, *, explicit=None, spec=None) -> (name, source)`
  in `harness/agent_kernel/kernel.py` is the single authority. Precedence is
  explicit argument → `agent_strategy` config → `RunSpec.strategy` → `"daily"`,
  and the returned `source` is recorded on `run_started`/`strategy_selected`.
- **`agent_strategy` is now honored.** It was previously set by
  `cli/commands.py:780` (`mode_config`) and `cli/interactive.py:5092` and read
  by NOTHING — every mode silently ran the same strategy. The kernel's mode
  names (`plan`, `build`, `explore`, `review`, `debug`, `ask`) are accepted as
  strategy aliases, so the CLI keeps working unchanged. An unknown name raises
  `ValueError` before a run directory is created; it is never a silent default.
- `harness.core.run_task` → `VerifiedFixStrategy`; `harness.agent_loop.run_agent`
  → `SessionController` + `legacy_agent`. `_run_task_legacy` and
  `_run_agent_legacy` are private compatibility adapters reached only through
  the kernel. `Task`, `TaskResult`, and the six-key `state.json` prefix are
  unchanged.
- New public exports: `STRATEGY_NAMES`, `resolve_agent_strategy`,
  `ConversationMemory`, `ConversationHandoff`, `TurnLedger`,
  `replay_turn_ledger`, `turn_ledger_path`.

### 2. Conversation continuity — `harness/agent_kernel/conversation.py` (NEW)

`ConversationMemory` owns one run's rolling message list.

- The base `[system, user]` frame is seeded ONCE per run and never rebuilt.
  A later turn's prompt always has the previous turn's prompt as a prefix.
- Prior assistant replies, tool results, and harness notes are retained inside
  the budget. Turns dropped past it are folded into a `ConversationHandoff`
  (turn range, tool counts, paths touched, per-turn findings, failure count)
  rendered as ONE structured user message ahead of the retained turns. The
  full records remain in `trace.jsonl` and `turns.jsonl`.
- The most recent tool result is never dropped, even alone over budget.
- Live workspace state (changed files + bounded diff) rides as its own turn via
  `record_state`, appended only when its digest actually changes.
- Budget keys (all in `harness/config.py`): `agent_conversation_messages` (48),
  `agent_conversation_chars` (24000), `agent_conversation_tool_chars` (4000),
  `agent_conversation_handoff_chars` (4000).

### 3. Durable per-turn state — `harness/agent_kernel/turns.py` (NEW)

- `TurnLedger` appends one compact record per turn to
  `logs/{run_id}/turns.jsonl` with an explicit `flush()` + `os.fsync()`, so a
  hard kill immediately after a checkpoint cannot lose the turn. Written on
  EVERY checkpoint, not only at completion; the final record carries the
  terminal status.
- Exactly one record per turn index: the run loop owns the per-turn checkpoint
  (`_execute_calls` no longer checkpoints, which had produced a duplicate
  turn-1 record).
- `replay_turn_ledger(path)` projects a ledger into a deterministic turn
  summary with no model call and no tool execution, so replay and a live run
  agree by construction.
- A resumed run reads the ledger back and states the recovered pre-kill state
  in the model's own message list (`_resume_brief()`), so resume continues the
  work instead of restarting the conversation.
- Write failures never kill the run: they surface as `turn_ledger_warning`
  events plus `TurnLedger.warnings`.

### 4. Honest completion

- `harness/agent_loop.run_agent` no longer maps `completed_unverified` onto the
  historical `success` word. `_compat_status()` preserves
  `completed_unverified` verbatim; only `completed_verified` yields `success`.
  The old code (`status = "success" if result.status == "completed_unverified"
  else legacy_status`) was a live violation of the invariant.
- The CLI needed no change: `cli/runview.effective_terminal_status` was already
  fail-closed and now receives the honest value, rendering
  `COMPLETED · UNVERIFIED` instead of `SUCCESS`.
- `harness.core.run_task` maps `completed_unverified` → `failed`; only
  `completed_verified` may become `TaskResult.status == "success"`.

### 5. Cross-terminal repairs made here (tool catalog drift)

Terminal 04's canonical catalog rewrite landed mid-session and broke three
kernel assumptions. All are fixed rename-proofly (resolved through the
registry, never hardcoded):

- `ask`/`question` control flow. `ToolRegistry.validate` normalizes to the
  canonical name, so the strategy's literal `call.tool == "ask"` never matched
  and an explicit question was executed as an ordinary tool. The strategy now
  resolves control tools via `self._control("question")`.
- `_allowed_tools` sets are built with `_canonical_tool()` from
  `builtin_tool_specs()` instead of hardcoded words.
- `edit`/`rename`/`delete` now REQUIRE `expected_revision` and `apply_patch`
  requires `expected_revisions`. A model cannot know a content digest it never
  read, so `_bind_harness_arguments()` binds the CURRENT revision from the
  safe workspace at the VALIDATION boundary (not in the handler). A
  model-supplied digest is overridden, never trusted: stale-edit protection
  stays mandatory and typed edits work.

### Verification (real, this tree, `-p no:randomly`)

- **Required command** `python -m pytest tests/test_agent_kernel.py
  tests/test_agent_loop.py tests/test_config_trace_state.py -q` → **123 passed**,
  exit 0 (baseline was 25 failed / 98 passed).
- `tests/test_agent_kernel.py` alone → **47 passed** (baseline 33 passed /
  5 failed). New coverage: turn-1-fact-required-on-turn-5, turn-prefix
  stability, structured handoff, REAL hard-kill + resume via
  `tests/kernel_kill_driver.py` (`os._exit(70)`), per-turn ledger append,
  default-config session/checkpoint artifacts, DONE → `completed_unverified`,
  and strict-vs-compat verification-contract parity.
- CLI regression: `test_cli_tui test_cli_session test_cli_tracelog
  test_cli_runview` → **254 passed** (baseline for the first three: 194).
- Harness/evals selection: `test_modes test_stubs_and_deps
  test_workspace_security test_daily_driver_evals test_evals_run
  test_evals_tasks test_scheduler_integration` → **237 passed** (after the
  evals probe fix below).
- `python -m evals.run --suite prompt-regression --check` → **14/14 CLEAN**.
- `ruff check` clean on all owned/edited files; `ruff format --check` clean on
  the new files. `kernel.py`/`strategy.py`/`agent_loop.py` hold pre-existing
  whole-file format debt from parallel work — only my own hunks were formatted.
- `python -m compileall -q` clean; `git diff --check` clean.

### Not run / honest blocked status

- **No Docker lane was run this round.** The real verifier lane
  (`tests/test_e2e_run_task.py`) and the full Docker prompt matrix were not
  selected; they are not reported as passes.
- **No live-provider lane.** No credential was inspected, printed, or retained.
- `ruff format` over `harness/agent_kernel/{kernel,strategy}.py`,
  `harness/agent_loop.py`, and `evals/daily_driver.py` was intentionally NOT
  run wholesale — those are shared dirty files with parallel in-flight edits.

### Integration requests to other owners

- **T10 (evals):** `evals/daily_driver.py` line ~4503 pinned
  `result.get("status") == "success"` for the `agent_fetch_enabled` feature
  probe, which my contract change invalidated (the run correctly completes as
  `completed_unverified`). I changed that ONE assertion to accept any
  completed status and added
  `agent_run_succeeded_unverified_not_dressed_as_success` so the probe still
  fails if an unverified run is ever reported as `success`. Please review that
  edit; it is the only change I made outside my ownership.
- **T11 (security):** during my first required-suite run,
  `test_trace_redacts_nested_credentials` failed with
  `assert '[REDACTED_SECRET]' == '[REDACTED]'`. Root cause was
  `shared/security.py` being edited mid-run (mtime 00:01) — not a harness
  defect. It passes now; no action needed, recorded for traceability.
- **T04 (tool catalog):** please keep aliases. The kernel now resolves control
  and mutation tool names through `ToolRegistry.canonical_name()`, so a rename
  is safe, but the catalog's REQUIRED `expected_revision` /
  `expected_revisions` arguments are a model-facing contract change worth
  documenting in `INTERFACES.md`.
- **T05/T06 (context/compaction):** `harness/agent_kernel/context.py` is
  UNCHANGED by this round. `ConversationMemory` is a separate module
  (`harness/agent_kernel/conversation.py`) specifically so ceiling prompt 02
  can own context budgeting without a merge conflict. `ContextBuilder.build()`
  is still called once per turn by `_messages_for_turn`, but only to refresh
  session state/prior diff — its returned messages are used ONLY on the first
  turn to seed the rolling list.

## Resume identity closure (2026-09-25)

Strict daily, verified-fix, and legacy-agent checkpoint writes now bind the
canonical repository, exact request hash, repository revision, and run
namespace. Resume validation rejects missing or mismatched identity before a
model or tool runs; the explicit `continue` compatibility request reuses the
stored request hash.

## Authoritative agent-kernel architecture (2026-09-25)

## Strict gateway usage parity (2026-09-25)

`ModelGateway` now falls back to the defining router module's
`get_last_usage()` when the callable itself has no usage reader, matching the
legacy `ModelClient` path. Strict responses canonicalize both `cost` and
`cost_usd`, so real router token/cost data is no longer recorded as zero.
The focused kernel regression passed.

Terminal 01 now has one public architecture rather than parallel fix/general
loops. `shared/agent_contracts.py` is the canonical Boundary-0 contract for
versioned `SessionState`, `RunSpec`, `ToolCall`, `PermissionDecision`,
`RunEvent`, `Checkpoint`, `RunResult`, and the exact seven-value
`CompletionStatus`; `harness.agent_kernel` re-exports those types.

`AgentKernel` owns session/run start, explicit strategy selection, one
append-only contiguous `trace.jsonl`, terminal result emission, cancellation,
and `SessionController` serialization. Registered strategies are `daily`,
`verified_fix`, `planning`, `question`, `research`, and `legacy_agent`;
unknown names fail before a run directory is created. Planning, question, and
research use the same context/model/tool loop with read-only or plan-only tool
sets. A model `finish`/`done` without clean verifier evidence is
`completed_unverified`, never `completed_verified`; timeout, cancellation,
blocking, and input requests are distinct statuses.

`harness.core._run_task_legacy` is now private. Public
`harness.core.run_task` constructs a verified-fix `RunSpec`, invokes the kernel,
and returns the exact historical `TaskResult`; the legacy loop writes through a
`JournalTraceLogger` into the kernel's same event file and remains the sole
writer of the six-key `state.json` prefix. Public
`harness.agent_loop.run_agent` similarly routes through `SessionController` and
the `legacy_agent` strategy, then preserves the historical CLI result shape
while adding `kernel_status`. The direct strict adapter remains available via
`run_agent_kernel` / `agent_kernel_enabled=True`.

`RunEventJournal` uses durable appends, a short cross-process lock, redaction,
and contiguous sequence allocation. `replay_run(path)` validates schema,
identity, sequence continuity, and produces a deterministic semantic projection
without calling a model or executing a tool. Checkpoint/session writes are
atomic; checkpoint selection no longer mutates its own root. Safe backend writes
now carry an expected revision, invalid Python edits roll back by operation ID,
searches refuse protected paths, and per-run cancellation no longer kills global
processes.

### Verification

- Required command: `python -m pytest tests/test_agent_kernel.py
  tests/test_agent_loop.py tests/test_config_trace_state.py -q -p no:randomly`
  -> **111 passed**.
- Expanded kernel regressions: **35 passed**, covering all contract versions,
  all completion statuses, strategy isolation, unknown-strategy fail-closed,
  deterministic replay/tamper rejection, resume, session lifecycle, safe
  overwrite/rollback, Boundary-2 gateway signature compatibility, core
  `TaskResult`/`state.json` preservation, and no kernel-to-CLI imports.
- Real Docker verifier lane: `python -m pytest tests/test_e2e_run_task.py -q
  -p no:randomly` -> **28 passed** in 391.22s.
- `python -m ruff check` passes for all owned Python files. New/shared/kernel
  files also pass `ruff format --check`; formatting the two large pre-existing
  dirty files wholesale was intentionally avoided in the shared worktree.
- `git diff --check` passes. `graphify update .` rebuilt 22,579 nodes, 66,028
  edges, and 4,757 communities.
- Router/mode compatibility: `python -m pytest tests/test_modes.py -q -p
  no:randomly` -> **99 passed** after the parallel CLI owner completed its
  signature-adaptive dispatch fix.
- Runtime caller compatibility: `python -m pytest
  tests/test_scheduler_integration.py -q -p no:randomly` -> **38 passed**.

### Integration requests / not yet wired outside this ownership

- **CLI/TUI owner:** migrate interactive session persistence fully to
  `SessionController`; today the kernel session file is authoritative for
  kernel turns while `cli/session.py` still keeps its compatibility transcript.
- **Router/modes owner:** route router `planning`/`question`/`research` entries
  to the registered strategies. The strategies are implemented and tested;
  `harness/router.py`, `qa_mode.py`, and `research_mode.py` were outside this
  terminal's ownership and remain compatibility entry points.
- **Build owner:** `build_mode.py` still creates a pre-stage `TraceLogger` and
  re-appends legacy rows after `run_task`. Change it to emit through the kernel
  journal (or pass an injected journal) so build journals remain fully
  versioned and replayable; do not append raw rows to `trace.jsonl`.
- No live-provider lane was run; no credential was inspected or retained.

## Product-round daily-driver kernel (2026-09-24)

The strict shared architecture is implemented under `harness/agent_kernel/`.
`RunSpec`, `ToolCall`, `PermissionDecision`, `RunEvent`, `Checkpoint`, and
`RunResult` are serialized contracts with explicit schema versions. The
`AgentKernel` composes `ContextBuilder`, `ModelGateway`, `PolicyEngine`,
`ToolRegistry`, `WorkspaceJournal`, `CheckpointStore`, `CompletionPolicy`, and
`RunEventJournal`. `SessionController` is the session facade; it has no global
live-run registry and exposes the same event callback to any renderer.

`DailyCodingStrategy` treats `finish` as a request: a declared verifier must
pass for `completed_verified`, while no verifier yields
`completed_unverified` with the exact checks recorded. Typed calls are
validated before policy evaluation; policy supports tool/path/command/MCP/
network/side-effect rules and once/exact-call/session-path/session-command/
global approval scopes. VCS metadata, secret-like files, harness/runtime
paths, configured protected paths, and protected shell references are hard
denials that cannot be laundered by an allow rule or approval grant. Session
state is atomic, bounded, compaction-aware, and explicit about corrupt or
unwritable state. Tool feedback is carried into the next model turn and
compacted session summary. The authoritative daily trace is `trace.jsonl`; rows
contain both normalized event fields and legacy `kind`/`data` aliases, and the
optional shared tracing overlay is emitted without becoming a second authority.

`AgentKernel` now builds the public `execution.workspace.Workspace` and
`SafeToolBackend` for typed edits/writes and normal shell/process calls. Local
processes use the execution workspace's bounded, scrubbed cancellation path;
`sandboxed=True` is an explicit configuration choice. The legacy handler keeps
the public `start_local_execution` fallback only for environments where the
execution backend cannot be constructed, and records that fallback as a trace
warning. `DailyCodingStrategy.cancel()` cancels the active token and requests
execution-process cleanup.

`harness.agent_loop.run_agent_kernel(...)` is the strict adapter. The legacy
`run_agent(...)` remains the default CLI-compatible path, but now scans and
injects applicable project skills into the first model request and emits the
`skills` receipt; the strict adapter carries the same skill block through
session metadata. Both paths preserve MCP/tool failure feedback for the next
model turn. The verified-fix strategy delegates to the unchanged
`harness.core.run_task` and translates its `TaskResult` without changing `Task`,
`TaskResult`, or the stable `state.json` prefix.

### Current verification

- `python -m pytest tests/test_agent_kernel.py tests/test_workspace_security.py -q -p no:randomly`: **88 passed** after safe-backend revision binding.
- Post-fix `dd_02_dirty_small_edit` and `dd_14_stale_edit_preservation` each passed baseline and adversarial arms.
- The current affected suite split is **161 passed with 1 platform skip**: 36 kernel, 62 agent-loop, 33 skills, and 30 CLI-session tests.
- `python -m pytest tests/test_agent_kernel.py tests/test_evals_run.py tests/test_daily_driver_evals.py -q -p no:randomly`: **97 passed**.
- `python -m pytest tests/test_evals_tasks.py tests/test_daily_driver_evals.py tests/test_evals_run.py -q -p no:randomly`: **69 passed**.
- `python -m evals.run --suite daily-driver --json --out-root C:\Users\pavan\AppData\Local\Temp\neo-terminal11-full-final`: **52/52 daily-driver arms passed**, including the real Docker canary; this report predates the final revision-binding patch, whose affected dd_02/dd_14 arms were rerun successfully. Report:
  `C:\Users\pavan\AppData\Local\Temp\neo-terminal11-full-final\20260925-005358-00a4cabf64ac40b89354ce59192ea05a\daily_driver_report.json`.
- `python -m evals.run --suite prompt-regression --check --out-root <isolated-root>`: **14/14 CLEAN**.
- `python -m ruff check` and `python -m compileall -q` pass for the owned kernel, eval, and session surfaces.

### Integration requests / unresolved blockers

- Safe workspace `edit` and `apply_patch` handlers bind the current `FileRevision` before dispatch, so normal typed mutations work without disabling stale-edit protection.
- The LSP public diagnostics boundary is still absent, so `dd_26_lsp_diagnostic_repair` is an explicit blocked capability and is not counted as a pass.
- The real-provider lane was not selected; no credential was inspected or retained.
- The existing real-development report has no explicit boolean manual-repair observations, so the sampled readiness gate remains incomplete rather than inferred.
- REPL/TUI context propagation is wired through the compatibility session facade; full migration of every interactive dispatch to `SessionController` remains an integration follow-up.

## Agent demo-parity round (2026-09-21) — resume replays, fetch already live

**`run_agent` gains additive `resume_history` (history replay, not a
restart); new `load_resume_history(task_id, log_root)` rebuilds the
prior-session preamble (request + files touched + recent exchanges)
from the run's own trace.jsonl — never raises, "" when nothing to
replay. The history injects as a steering-context user message
(at=agent-resume-history, same trace kind as plan guidance) so the
loop starts from the current tree with earlier work visible.
Pristine/orig discipline unchanged (snapshot-once, orig-once — a
resumed same-id run keeps both, test-pinned). Fetch/MCP/plugin verbs,
approval_required/decided tracing, and the require-without-approver
honest refusal are unchanged (already built in the trust round).
`neo fix` modules untouched (core/editor/tools/prompts/router/verify/
sandbox byte-identical to this round — no edits there).

### Verification

- tests/test_agent_loop.py: new TestAgentResumeHistory (4 tests:
  replay content, empty-on-missing, steering injection reaches the
  model + trace, same-id pristine/orig kept).
- demo/agent_demo.py drives the loop offline (scripted model, local
  tools only) through question -> mention -> plan -> approval ->
  undo -> resume -> compact, exit 0.

## General agent loop (2026-09-21) — `harness/agent_loop.py`, the interactive engine

**The interactive `neo` session is now a general coding agent; `neo fix`
(the verifier-gated benchmark path through `core.run_task`) is UNCHANGED
— same loop, same verification, same git output. The session used to
dispatch four modes (fix/question/build/research via harness.router);
it now classifies question | agent_task | chit_chat and runs ONE tool
loop for everything work-shaped (fix/build/refactor/run/debug).**

### What's built

- **New module `harness/agent_loop.py`** (only new harness surface):
  - `classify_agent_input` / `classify_deterministic` — deterministic
    rules (run verbs, fix verbs, build verbs, research markers,
    explain/question shapes, chit-chat openers) + ONE cheap model call
    (`difficulty_hint="easy"`) for the gray zone only; any model
    failure degrades to chit_chat with a clarifying reply (never
    launches). `agent_intent_enabled=False` = everything is agent_task.
  - `run_agent(request, repo_path, config, log_root, task_id,
    approve_fn, on_event)` — the loop, on the LIVE repo (edits in
    place; no pristine/work build copies). Tools: READ/GLOB/GREP/BASH
    (via the existing `BashSession` → `execute_sandboxed` boundary,
    deny-guard included)/EDIT (exact-block replace)/WRITE/MEMORY
    (decision_memory query)/VERIFY/DONE. Model protocol: one JSON
    `{"tool": ...}` (fenced or bare) or one plain line per reply;
    unparseable replies get one retry nudge, never a crash.
  - Steering (`harness/steering.py`, reused as-is) polled every turn:
    guide injects into the live messages, abort stops cleanly,
    replan rides as strong guidance (no fixed plan to replace).
  - NO verifier gate by default — DONE mints success; `verify()` runs
    only when `target_test`/`test_command` are set (or on explicit
    VERIFY). Stopping: `agent_max_turns` (25), budget cap, wall-clock.
  - Diff/undo: `logs/{task_id}/pristine/` (snapshot at start, reference
    ONLY) + `orig/` per-file originals stashed before first edit;
    `agent_diff` (unified diff vs live repo) and `undo_edits` (newest-
    first restore; agent-created files deleted on `undo all`).
  - Trace: same public kinds the CLI renders (task_start mode=agent,
    retrieval, decision_memory, model_request/response via ModelClient,
    tool_call/tool_result, verify, steering*, task_end, result) plus
    additive `edit_applied` / `approval_required` / `approval_decided`.
  - Permission model: reads always run; BASH/EDIT/WRITE need
    `approve_fn` approval only when `agent_approval="require"`
    (no approver = honest refusal, never silent).
- **Config keys** (all additive in config.py): `agent_max_turns`,
  `agent_approval`, `agent_context_files`, `agent_context_lines`,
  `agent_max_read_chars`, `agent_intent_enabled`.
- **Untouched**: `core.py`, `editor.py`, `tools.py`, `router.py`,
  `intent.py`, `qa_mode.py`, `build_mode.py`, `research_mode.py` —
  the benchmark path and all mode modules are byte-identical.

### Decisions worth knowing

- Research-shaped input ("research how X compares...") maps to
  **question** (read-only Q&A) — the agent loop has no web FETCH; the
  legacy research entry stays for programmatic callers that need it.
- `/plan <text>` and `/review`-with-template stay on the fix loop in
  the CLI (preview machinery lives in `_execute_task`; the agent loop
  has no preview) — deliberate, documented in cli/AGENTS.md.
- `BashSession` is reused (not raw `execute_sandboxed`) so the deny
  guard, cwd tracking, and output caps behave exactly like the fix
  loop's; traversal-unsafe READ/EDIT/WRITE paths are refused
  harness-side before any I/O.
- Classification order matters: run > fix > research > question-shape
  > build > artifact-declarative > unknown. "Research the crash" is a
  task (fix wins); "what should I add?" is a question (shape wins
  over the build verb).

### Verification

- tests/test_agent_loop.py: **32/32** (classification matrix incl. the
  three dogfood sentences, tool parsing, DONE/READ/EDIT flows on a tmp
  repo with diff+undo, fake-sandbox BASH, verifier on/off, approval
  require allow/refuse, steering abort, traversal refusal, trace file).
- Regression: test_modes 97+2skip, test_steering 35+6skip (Docker-gated
  skips — daemon down machine-wide at round time), test_cli_tui 43/43,
  test_cli_neo3 + tracelog + agent 148, CLI sweep 346+1fix (the 1 was
  the custom-command dispatch update, fixed in-test).
- Docker-down environmental failures (pre-existing suites needing the
  daemon: test_cli fix e2e, release --json): fail at baseline verify
  before any touched code runs; re-run when the daemon is back.
- ruff: harness/agent_loop.py + tests/test_agent_loop.py clean;
  interactive.py/tui.py held at their pre-existing finding sets (all
  flagged lines are pre-existing regions).

### Not yet implemented / honest notes

- Agent resume (`/resume` on an agent task) restarts the loop under
  the same task id from the current tree — message history is not
  replayed, but pristine/orig references are kept so diff/undo stay
  coherent (undo appends an `undo` trace event; per-file
  `/diff undo <file>` and `undo all` with created-file deletion work
  post-resume because orig/ is never wiped).

## Agent trust round (2026-09-21) — approval modal path, plan preview, undo, MCP/plugin/fetch

**The four "Not yet implemented" items from the section above are now
built (only the message-history-replay caveat on resume remains, by
design — the loop starts from the current tree).**

- **Tools**: `fetch` (read-only web, GET-only SSRF-guarded + capped +
  budgeted via `agent_fetch_enabled`/`agent_max_fetches`; BASH never
  shells a FETCH line), `mcp`/`mcp_call` (plugin servers + config
  `agent_mcp_servers`; failures -> TOOL ERROR kinds, never traceback),
  plugin verbs through one `route_tool` (builtin|mcp|plugin|unknown;
  read-only shapes auto-run even under require; unknown tools honest
  errors). `parse_tool_call` takes optional `known_verbs` (default
  strict). BASH runs LIVE (local subprocess, never Docker; injected
  fakes still win; deny-guard + cwd + caps kept; Ctrl+C stops the
  call, never the session).
- **Plan**: `render_agent_plan` (heuristic steps + retrieval files, no
  verifier fabrications); approved plans inject as `plan_guidance`
  steering (replan-path semantics), never a fixed contract.
- **Undo**: per-file `targets=` + `undo all` (created files deleted) +
  `undo` trace event; pristine/ never touched.
- **Config** (all additive): `agent_fetch_enabled`, `agent_max_fetches`,
  `agent_live_bash`, `agent_mcp_servers`.
- **Verification**: tests/test_agent_loop.py 56/56 (plan, fetch incl.
  budget, MCP incl. live-server drive, plugin verbs incl. require-mode
  auto, undo incl. trace event + pristine intact, Ctrl+C BASH,
  REPL preview approve/edit/cancel); live drive
  (Temp/opencode/drive_agent_trust.py): require-mode approve+deny+undo,
  plan->edit->DONE, real `python -m mcp_server` query_decisions through
  the loop; `neo fix` modules untouched (core/editor/tools/prompts/
  router/verify/sandbox); evals --check OK.
- **CLI surfaces** (see cli/AGENTS.md): TUI `_agent_approve_fn` modal
  (y=once/a=always/n), `/plan` agent preview, `/diff undo <file|all>`.

## Mid-Task Interactive Steering round (2026-09-14) — steering a live task (Tasks A-C)

**New user instructions injected while a task runs — "actually, only
touch file X", "stop, that's the wrong approach" — WITHOUT losing the
task's progress.** New module `harness/steering.py`; loop consumption
points wired in `core.py`; REPL reader + registration in `cli/interactive.py`
(see cli/AGENTS.md for the surfaces). One mechanism, three surfaces
(REPL plain-text, TUI plain-text//steer, cross-process journal writes —
a second terminal can inject by appending to the journal).

### The mechanism (Task A)

- **Transport**: `logs/{task_id}/steering.jsonl`, an append-only journal
  (inject + consume records, one JSON object per line). ANY process can
  inject; the loop polls at safe checkpoints. CROSS-INSTANCE
  CORRECTNESS (the round's fatal pre-fix bug): the CLI-side injector
  buffer and the loop's buffer are different objects — every consumer
  poll (`pending`/`take`/`steering_context`/`inject`) RE-SCANS the
  journal first (`refresh()`; binary-mode incremental scan keyed by a
  byte cursor — torn tails retried, shrunk journals rebuilt). Without
  this, steering never reached the running task.
- **Intents are explicit, parsed at INJECT time** (`parse_steering_line`):
  plain text → `guide`; `replan:`/`replan <text>` (or bare `replan`) →
  `replan`; `abort`/`abort:` → `abort`. Unknown/empty → guide (the
  least-destructive intent; a wrong guess must never abort a run).
- **Safe checkpoints (consume points)**, innermost first: (1) TURN
  boundaries in `run_step` — between model calls, guide events inject a
  `USER STEERING` message into the LIVE session (the model continues
  with the instruction visible); strong intents end the step
  (`STEER-REPLAN:`/`STEER-ABORT:` note) so (2) the STEP-boundary handler
  acts: abort → clean resumable stop; replan → the shared re-plan path;
  (3) the FINAL-GATE check before the final verify; (4) the PRE-MINT
  re-check after verify/agent-tests/self-critique all pass but before
  success is minted (closes the window where steering arrives DURING
  those long-running stages).

### State machine interaction (Task B)

- A steering interrupt is NOT a state transition — `machine.record_event`
  ("steering"/"steering_replan"/"steering_abort") appends a non-transition
  audit record (phase unchanged) to transitions.jsonl, so the trail shows
  WHEN the user redirected without ever corrupting a valid edge.
- The two strong intents move the machine through EXISTING forward
  edges only: replan = editing → repairing → planning (`_do_steering_replan`:
  the attempt is dismantled for a new plan; work-in-progress KEPT —
  `keep_work_next`, same exemption shape as a resume's first iteration;
  the attempt slot is given back); abort = `<current>` → `failed` via
  `_steering_abort` (every non-terminal phase allows the failed edge;
  without the transition the live-status phase would stay stale forever).
- The re-plan prompt carries ALL steering (consumed + pending —
  `steering_context`, capped by `steering_max_chars`) PLUS the current
  work diff, and the section sits AFTER `## Retrieved context` so the
  difficulty predictor's first-message cut is unaffected.

### The guarantees (Task C) — all test-pinned

- **Verifier-gated completion is never shortcut**: pending steering at
  the final gate or the pre-mint checkpoint BLOCKS success minting —
  the attempt is poisoned (`success minting deferred` decision) and the
  fix is re-verified after the steering is incorporated. Guide rides
  the next attempt's feedback; replan replaces the plan. Success is
  still only ever minted on a real verifier pass.
- **Checkpoint/resume is intact after steering**: an inject without a
  matching consume is STILL PENDING after a crash (the journal replays
  on buffer construction) — a steered task interrupted after steering
  resumes with the steering intact. A steered abort leaves work/,
  state.json, plan.json, and the journal — resumable via `neo --resume`.
- **The OFF arm is one code path**: `steering_enabled: False` → the
  loop's buffer is None → the journal is never polled (an injected
  abort sits unconsumed; the run finishes as if nothing was typed).

### Config keys (all additive, in config.py)

`steering_enabled` (True — the OFF arm), `max_pending_steering` (16 —
bounded queue; over-cap injects are REFUSED honestly, never silently
dropped), `steering_max_chars` (4000 — re-plan context cap).

### Verification at close

- tests/test_steering.py: **41/41** — parse matrix; buffer unit
  (journal/replay/cap/torn-tail/shrink/thread-safety); CROSS-INSTANCE
  transport incl. a real second-process inject; state-machine
  `record_event` phase-neutrality + replan forward-edges + abort
  landing; `steer_live_run` (ack/journal/convo-refusal/say-hook);
  `_execute_task` registration + crash-clear; Docker-gated e2e through
  the REAL loop: guide at a turn boundary (consumed, fix still
  verified), abort at a step boundary (failed + resumable artifacts +
  machine failed), replan (plan_replaced + work kept + steering in the
  re-plan prompt), final-gate guide (pre-mint block, attempts ≥ 2,
  deferred decision, eventual honest success), OFF arm (journal
  unconsumed, success), resume-after-steering (journal replay).
- TUI behavior pinned in tests/test_cli_tui.py (`/steer` in help;
  in-flight plain text steers + journal write; live-run registration).

### Honest notes / known limits

- A journal replaced in place with same-or-larger content (a pathological
  archive collision) is not detectable by size — the shrink guard covers
  truncation; the real archive path moves the whole directory, so this
  cannot occur in practice (test documents the behavior).
- Steering during the baseline verify (pre-plan) is consumed at the
  first turn boundary of the first step — no earlier consume point
  exists, by design (planning must not be interrupted mid-model-call).
- Steering typed BEFORE the loop starts (a build's stage-1 window) is
  refused honestly by the CLI's live-gate (trace.jsonl must exist —
  see cli/AGENTS.md); the harness never sees it, by design (the
  pre-loop journal would be archived on the loop's fresh start).
- `max_pending_steering` refuses the 17th concurrent pending
  instruction; the user sees an honest "steering refused" ack.

## Proactive Codebase Health Scan round (2026-09-14) — `neo scan` (Tasks A-C)

**A new read-only scan mode — genuinely new autonomous-initiative behavior,
not a reactive fix: without a bug report, analyze a repo and surface what's
actually worth a developer's attention.** New module `harness/scan_mode.py`;
CLI subcommand `neo scan` (cli/main.py, additive); config keys +
`--finding` handoff in `fix`. Read-only BY CONSTRUCTION: no shell, no
sandbox, no editor, no pristine/work copies, and NO model calls — findings
come from the graph + AST + manifests, ranked deterministically from each
finding's own evidence (the rationale-log discipline, no invention).

### Task A — run_scan (the analysis)

- Three detectors: **coverage gaps** (the EXISTING memory.code_graph via
  harness.deps — module-level gaps by test-import/call over-approximation
  plus a one-import-hop indirect-coverage rule that kills the
  helper-module false-positive class the first live run exhibited;
  function-level gaps only for load-bearing symbols ≥2 non-test call
  sites inside otherwise-covered modules), **latent-bug smells** (stdlib
  AST; only the three classes with real failure histories — mutable
  defaults, bare except, swallowed exceptions; style nits deliberately
  out of scope), **dependencies** (pin conflicts across manifests
  offline + opt-in PyPI freshness via `--remote`/`scan_remote_deps`,
  network OFF by default exactly like `docs_lookup_allow_remote`;
  "known issues" NOT claimed — the honest signal is version distance).
- Read-only invariants are TEST-PINNED (tests/test_scan_mode.py
  TestReadOnlyInvariants): no BashSession/execute_sandboxed imports, no
  writes inside the scanned repo, graph index built OUTSIDE the repo.
- Honest degradation: no .py sources → coverage/smell skipped with a
  note; missing graph layer → coverage skipped, never an exception; a
  repo that is not a directory → status "error" with a note.
- Artifacts per scan: `logs/scan-<id>/` with `scan.json` (ALL ranked
  findings, `index`-stamped), `report.md`, `trace.jsonl` (task_start /
  scan_detector×3 / scan_summary / task_end).

### Task B — ranking + rationales (the noise budget)

- Findings scored (severity × kind weight + structural signals: fan-in,
  call-site count) and sorted; the report shows only the top
  `scan_max_findings` (8) — the rest stay in scan.json, resurfaced via
  `neo scan --max-findings N`. **The first live runs found 101–1323
  smell sites and 20+ coverage candidates — the noise budget is what
  makes the output useful**; `scan_smells_per_kind` (3) and
  `scan_func_gap_max` (3) cap per-kind output, preferring
  structurally load-bearing files.
- Every finding carries a 2-4 sentence rationale in the fix-round
  rationale-log style: what was found, why it deserves attention, what
  the first step is — deterministic from the finding's own evidence.
- CLI: `neo scan --repo . [--focus coverage|smells|dependencies]
  [--remote] [--max-findings N] [--json] [--fix N]`; `--fix`/
  `--json` mutually exclusive; `--focus` validates its choices.

### Task C — the handoff (`neo fix --finding <scan_id>#<n>`)

- Every finding carries a fix contract (`fix_kind` / `fix_issue_text`
  / `fix_target_test` / `fix_note`): coverage/smell findings route
  through the plain fix loop with a suggested NOT-YET-EXISTING target
  test (baseline verify fails honestly; the issue text explicitly
  authorizes test-writing, squaring the loop's don't-touch-tests rule);
  dependency bumps route through build mode (a version-floor acceptance
  test genuinely fails on the old pin).
- `resolve_finding` runs the scan id through memory.paths.safe_task_dir
  (traversal-shaped ids rejected before any filesystem use); the index
  is 1-based against the ranked order the report printed.
- E2E-PINNED through the REAL loop (Docker-gated
  TestScanFindingToTaskE2E): `neo scan` notices an untested module →
  `neo fix --finding <id>#1` → cmd_fix → run_task → REAL Docker verify
  → success, with the finding's own test file as the target.
- `neo scan --fix N` closes the loop in one command: scan, pick, hand
  off.

### Validation against a real repo (the "genuinely useful, not noisy" gate)

Final run against THIS repo (`neo scan --repo .`): **7 findings, 7
shown, 0 suppressed, 122.8s, exit 0** — 4 coverage gaps (`cli/ui.py:
err_console()` 20 call sites; `execution/sandbox.py: docker_available()`
10; `memory/paths.py: default_logs_dir()` 12; `cli/uninstall.py`
whole module) + 3 swallowed-exception smells in execution/sandbox.py —
every one spot-checked genuine (no tests exercise those symbols; the
smell lines are real `except: pass` handlers). Iteration history that
shaped the caps: early runs surfaced 1323 raw smell sites / 20+
coverage candidates; per-kind caps + fan-in ordering + the 8-finding
budget reduced that to the 7 worth attention. Report at
`logs/scan-6045bc18/report.md`.

### Config keys (all additive, documented in config.py)

`scan_max_findings` (8), `scan_remote_deps` (False),
`scan_pypi_timeout_s` (10), `scan_smells_per_kind` (3),
`scan_func_gap_max` (3).

### Verification at close

- tests/test_scan_mode.py: **50/50** (run_scan basics; coverage/
  smell/dependency detectors incl. mocked-PyPI remote; ranking +
  suppression; finding resolution incl. hostile refs; CLI scan +
  `--json`; `neo fix --finding` handoff; the Docker-gated e2e; the
  read-only invariants).
- Real-repo run: exit 0, 7 focused findings, spot-checked genuine.

### Honest notes / known limits

- Coverage "exercise" is an import/call over-approximation (the
  coordination fan-out's recall-over-precision trade) — a module
  imported by a tested module counts as indirectly covered (one hop
  only); a function hit ONLY by import side-effect isn't distinguished
  from a real behavior test. Precision could improve with coverage-
  tool integration — deliberately not built (a scan must stay
  instant, offline, read-only; no test execution).
- Detectors are Python-first; a repo with no .py sources reports
  honestly. JS/TS graph indexing exists upstream but the AST smell
  pass does not — documented limitation, not a silent skip.
- The dependency detector scans requirements.txt + [project]
  dependencies; other manifest shapes (setup.py, Poetry/PDM sections)
  are not parsed.

## Long-Horizon Planning round (2026-09-14) — multi-session build projects (Tasks A-C)

**Build mode planned within a single session; this round added the layer
ABOVE it for feature requests too large for one session: a PROJECT PLAN
of smaller, independently-checkpointed sub-tasks that span multiple
sessions — using the existing per-task machinery unchanged, but tracked
at a higher level (completed SUB-TASKS across sessions, not steps within
one).** New module `harness/build_plan.py`; prompts + config keys in
`prompts.py`/`config.py`; router dispatch in `router.py`. Build mode's
basic version (Modes round, Task D) existed first — this built on it, as
instructed.

### Task A — multi-session task decomposition (`run_project`)

Three stages, one or MANY invocations:

1. **Stage 1 — criteria extraction (Task B, see below)**.
2. **Stage 2 — decomposition**: ONE model call
   (`render_project_plan_prompt`) decomposes the feature into ordered
   sub-tasks, each mapped to the criteria ids it delivers. A plan whose
   sub-tasks don't cover every criterion is a HARD planning error (the
   contract can never complete — honest abort, never a partial effort).
3. **Stage 3 — execution**: each sub-task is ONE unchanged
   `build_mode.run_build` session — sub-request = the sub-task's
   description + its criteria sentences; start repo = the ORIGINAL repo
   for sub-task 1, the ACCUMULATED verified tree for later ones. On
   success the sub-task's verified `work/` is PINNED to
   `logs/{project_id}.tree-s<N>/` (a stable sibling — the sub-task's own
   task dir can be archived by a later re-run of the same id; the pinned
   tree must not) and becomes `current_tree` in the project file.

**The multi-session pause**: `project_sub_tasks_per_session` (default 1)
bounds BUILDS per invocation (a sub-task that completes
by-verification — its authored tests already pass — consumes no
budget); reaching the budget writes status `"checkpointed"` and
returns — a LATER session continues with `project_resume=True` + the
same project_id (completed sub-tasks are never re-run; the accumulated
tree carries forward; `sessions` counts invocations). A sub-task FAILURE
also checkpoints (completed set kept) — the next session retries just
that sub-task. A sub-task whose authored tests already pass on its
start tree is COMPLETE-BY-VERIFICATION (`already_exists` from run_build
is treated as done — for sub-task 1 on the original repo it means the
feature exists; for later ones it means an earlier sub-task delivered
those criteria early).

### Task B — acceptance-criteria extraction (the whole-effort contract)

BEFORE decomposition, ONE model call
(`render_project_criteria_prompt`) extracts explicit acceptance
criteria — id'd snake_case, one testable behavior sentence each, capped
by `project_criteria_max` (8). This is the completion contract for the
WHOLE effort: every criterion must be claimed by ≥1 sub-task (planning
gate) AND delivered by a completed one (final gate), and the project's
final gate re-runs a targetless full-suite verify on the accumulated
tree (the whole suite is the gate — per-sub-task regression gates
already ran; this is the project-level confirmation). Empty-reply
flake: ONE retry with a repair nudge, then honest error (the standing
discipline — qa/research/build_tests all do this now).

### The project plan format — `logs/{project_id}/project.json`

```json
{
  "project_id": "proj-x",
  "request_text": "<the original feature request>",
  "repo_path": "<ORIGINAL repo — never mutated>",
  "criteria": [{"id": "csv_export", "description": "..."}],
  "sub_tasks": [{"id": 1, "description": "...", "criteria": ["..."],
                 "files_hint": ["..."]}],
  "completed": [1, 2],          // sub-task ids verified done
  "current_tree": "logs/proj-x.tree-s2",  // next sub-task's start repo
  "sessions": 2,                 // run_project invocations so far
  "status": "active|checkpointed|success|already_exists|failed|error"
}
```

Atomic tmp+replace writes (state.json discipline — a crash can never
leave a partial file; pinned-trees are written BEFORE the project file
records them). Layout: project dir `logs/{pid}/`, per-sub-task builds
`logs/{pid}-s<N>/` (+ `.base` staging copies, the build_mode contract),
pinned trees `logs/{pid}.tree-s<N>` — all harness-owned logs-root space,
original repo untouched throughout (test-pinned).

### Task C — the proof (3 distinct module changes, 2 real sessions)

**Fixture `tests/fixtures/feat02_shop`** (new): a small catalog/cart
package where the request genuinely requires several DISTINCT changes:
receipts (new `shoplib/receipts.py`), loyalty points
(`shoplib/pricing.py`), CSV export (`shoplib/serializers.py`) — three
sub-tasks across three modules, not a single-file toggle.

**The e2e proof** (`tests/test_build_plan_e2e.py`, Docker-gated,
scripted model through the REAL loop + REAL Docker sandbox/verify at
every gate): SESSION 1 extracts criteria, decomposes (3 sub-tasks),
completes sub-task 1 (receipts) and CHECKPOINTS at budget 1 —
project.json on disk, pinned tree holds receipts.py + its acceptance
tests, original repo without them. SESSION 2 resumes
(`project_resume=True`): sub-task 2's base copy demonstrably contains
sub-task 1's receipts module (accumulation proven), completes loyalty +
CSV, and the project finishes — all 3 criteria covered, final
targetless full-suite verify green on the accumulated tree, exactly 3
`project_sub_task_start` events across the whole project (sub-task 1
never re-ran). Resuming a finished project is an honest no-op. Plus:
failed-sub-task retry-from-checkpoint (an impossible acceptance test
fails honestly; a later session retries sub-task 2 with a healthy model
and the project completes) and the coverage-gap planning abort.

### Wiring + config

- `router.py`: a build request with `build_project=True` dispatches to
  `run_project` (late import); otherwise the single-session
  `run_build` path is byte-identical. The CLI/session can set the key
  from config files per the two-tier settings convention (no new flag
  needed — it's a task.config value like every other knob).
- Config keys (all additive, documented in config.py):
  `build_project` (False), `project_max_sub_tasks` (4),
  `project_sub_tasks_per_session` (1), `project_criteria_max` (8),
  `project_resume` (False).
- Trace events (logs/{project_id}/trace.jsonl): `project_start`,
  `project_criteria_extracted|capped|parse_error|empty_reply_retry`,
  `project_plan_generated|capped|parse_error|empty_reply_retry`,
  `project_plan_saved`, `project_sub_task_start|end|already_passing`,
  `project_checkpoint`, `project_final_verify`, `project_end` — all
  additive, safe to surface in dashboards (T4).

### Verification at close (all real, not assumed)

- tests/test_build_plan.py: **20/20 offline** (criteria normalization +
  caps + empty-retry + parse-aborts; decomposition + caps + coverage
  gap; project-file roundtrip/atomicity/tolerance; router wiring both
  paths; config defaults; whole-project already_exists — all-passing
  verdict + no verify on the original repo, mixed build/passing with
  the final verify on the ACCUMULATED tree, resume-as-no-op).
- tests/test_build_plan_e2e.py: **3/3 Docker-gated** (the 2-session
  proof above; failed-sub-task checkpoint+retry; coverage-gap abort).
- Regression: test_modes 99 + test_build_plan 20 + test_config_trace_
  state 12 + test_e2e_run_task 28 = **159 green** (~10 min warm
  Docker); test_skills 27 + test_coordination 31 + evals_tasks 8 +
  cli-neo3+tui 82 green; `python -m evals.run --check` 14/14 OK (this
  round changed NO prompts the eval arms diff — the new prompts are
  project-layer only, orthogonal to the fix-loop arms).
- ruff: all NEW/touched files violation-free (`harness/build_plan.py`,
  `tests/test_build_plan*.py`, `router.py`, `config.py`, `prompts.py`).

### Honest notes / known limits

- The criteria contract is enforced at id level: a sub-task CLAIMS
  criteria ids and its own acceptance tests encode them; the harness
  verifies behavior via those tests per sub-task, plus the project-wide
  full-suite verify. It cannot prove a sub-task's tests were
  COMPREHENSIVE for their criterion — that's the same trust model as
  single-session build mode (agent-authored contract), one level up.
- `already_passing` for a sub-task marks it complete BY VERIFICATION
  and the project CONTINUES (one sub-task's passing evidence speaks
  only for its own criteria — a later sub-task may still have real
  work). Two consequences: by-verification completions don't consume
  the session budget (a verification is not a build — an
  all-already-passing project resolves in ONE session, not N), and
  when EVERY sub-task completes that way the whole-project verdict is
  `already_exists` with NO final verify: the only candidate tree would
  be the ORIGINAL repo, which the sandbox mounts read-write —
  verifying it would break the never-mutate guarantee for a verdict
  the per-sub-task verifier runs already established. A resumed
  `already_exists` project no-ops exactly like a finished `success`.
  (Pinned in TestProjectAlreadyExists: all-passing verdict, mixed
  build+passing with the final verify on the ACCUMULATED tree only,
  and resume-as-no-op.)
- Session budget vs wallclock: budget bounds SUB-TASKS per session, not
  time; a sub-task that times out forwards "timeout" and checkpoints
  (retryable) — the standard task-level semantics, one level up.

## Modes round (2026-09-13/14) — intent router & multi-mode capability (Tasks A-F)

**The scope expansion that makes Neo a general coding agent: one input
pipeline, four work modes. The fix-engine is UNCHANGED — build/question/
research are new ENTRIES around it, never loop variants.** New modules
`harness/intent.py` (Task A), `harness/router.py` (Task B),
`harness/qa_mode.py` (Task C), `harness/build_mode.py` (Task D),
`harness/research_mode.py` (Task E); wiring in `cli/interactive.py`
(session dispatch), `harness/core.py` + `context.py` (additive mode key),
`harness/config.py` + `prompts.py`.

### Task A — intent classification (harness/intent.py)

Two tiers, deliberately:
1. **Deterministic rules first** (offline, free, instant): greetings/
   meta-questions → convo; bug-language → fix; build-verb+object →
   build; research-verb+external-marker → research; question shapes →
   question. `hi` costs nothing and NEVER launches a task (the permanent
   fix of the original "hi"-bug — conversational input has a real path).
2. **ONE cheap model call for the gray zone only**: `classify_with_model`
   with `difficulty_hint="easy"` (adaptive routing picks the cheap tier;
   pin `intent_model` to force). ANY failure (endpoint down, empty,
   unparseable, unknown kind) degrades to `ambiguous` — the session
   ASKS one clarifying question instead of guessing (a wrong task-run
   burns minutes+model budget; a question costs one line).
- `intent_enabled=False` (config) = the OFF arm: everything is fix, the
  legacy pre-modes contract.
- Trace: `intent` {kind, reason, tier} on every classification; `route`
  {kind, reason} on dispatch.

### Task B — the router (harness/router.py)

`route(text, repo_path, config, ...)` — classify, then dispatch: fix →
`core.run_task` (unchanged); question/build/research → their modules;
convo/ambiguous → `ModeResult(status="reply", answer=...)` for the
SESSION to print, nothing launched. Thin dispatcher, NO policy beyond
mode selection; late imports so a broken optional mode never breaks the
import graph; `handlers=` override for tests. `route_kind` is the single
import site for the routing decision (the CLI session uses it).

### Task C — Q&A mode (harness/qa_mode.py)

Read-only, no sandbox, no pristine/work copies, nothing to verify:
retrieval (structural+grep, read-only) + decision memory assemble the
context block; ONE model call (plus bounded `READ <path>` round-trips —
a control signal parsed before command extraction, never executed;
traversal-refusing, capped) answers directly. Empty model reply (the
endpoint's documented reasoning-burn flake) → ONE retry with a repair
nudge, then honest `error` — success is NEVER minted on "".

### Task D — build mode (harness/build_mode.py) — the hard one

No pre-existing failing test exists for a new feature, so the completion
criterion is made REAL: **test-first contract**. Stage 1: ONE model call
(`render_build_tests_prompt`) authors the feature's acceptance tests
(same sanitize as fix-mode agent-tests); they're written into a PRIVATE
base copy of the repo (`{task_id}.base` — a SIBLING of the task dir, see
the real bug below); the baseline verify CONFIRMS they FAIL on the
pristine tree (a passing contract means the feature already exists —
reported honestly as `already_exists`, nothing built). Stage 2: the
UNCHANGED fix-engine runs with `target_test` = the acceptance tests —
baseline/regression/flake/agent-tests/self-critique/git output, all
verifier-gated. Build mode is a different ENTRY, not a different loop.
`state.json` gains the additive `mode` key (omitted for fix tasks; the
six Boundary-4 keys stay a strict prefix).

### Task E — research mode (harness/research_mode.py)

Read-only investigation of an external topic: the model drives
`FETCH <url>` (the proven webfetch reader — GET-only, SSRF-guarded,
capped, audited via `web_fetch` trace events) and `DOCS <target>`
round-trips, then synthesizes (direct answer + grounded key findings +
honest not-found line). No shell, no sandbox, no repo edits (test-pinned:
BashSession/execute_sandboxed absent from the module). Budgets:
`research_max_fetches` (4), `research_max_docs` (4), `research_turns` (8).

### Task F — the proof, and the five REAL defects it caught

**Driver: `logs/modes-round/four_modes_session.py`** — one process, one
config, the four canonical inputs + "hi", REAL cloud model
(z-ai/glm-5.3-free @ tokenrouter), REAL Docker sandbox/verify for fix +
build, REAL web fetches for research. Final run
`20260914-004700`: **19/19 checks PASSED**, $0.050 total, all four
modes verifier- or answer-gated, router distinguishing every input, "hi"
launching nothing. Report: `logs/modes-round/four_modes_report_20260914-004700.json`.

The defects the live session found (all fixed + regression-pinned in
tests/test_modes.py + test_webfetch.py):
1. **build_mode's base copy lived INSIDE `logs/{task_id}/`** — the fix
   loop's `_fresh_paths` archives that dir on every fresh start,
   sweeping the staged repo away before the snapshot (WinError 3, status
   "error"). Fix: stage at `{task_id}.base`, a SIBLING of the task dir.
2. **`library` (singular) never matched the external-marker regex**
   (`librar|libraries` + `\b` boundaries) — the num2words research
   question fell to the gray zone. Fix: `librar(y|ies)`.
3. **"an empty list must raise ValueError" — spec language in a build
   request tripped bug-language (`raised?`)**, misrouting build → fix.
   Fix: raise/throw count as symptoms ONLY in the compound form
   ("raises when/if/on") — a bare contract clause is not a bug report.
4. **Empty-reply flakes minted success**: qa_mode returned
   status="success" with answer="" (the endpoint's reasoning-burn
   window). Fix: ONE retry with a repair nudge (a DIFFERENT ask — the
   same prompt deterministically burns twice), then honest error; same
   discipline now in qa_mode, research_mode, AND build_mode's
   test-authoring call.
5. **Glued FETCH URL crashed the fetch**: a degenerate reply glued
   think-tag prose onto the FETCH line; the old DOTALL `parse_fetch`
   swallowed the tail into the URL → "URL can't contain control
   characters" → the whole research run errored. Fix: the URL is ONE
   token (`[^\s<]+` — `<` is the think-tag glue marker) + URL-charset
   validation; plus **degenerate-fetch salvage** in research_mode: a
   reply with no clean tool line that TRIED to fetch ("FETCH" appears +
   an explicit URL) gets the URL salvaged and executed — an attempted
   fetch must never silently degrade into an ungrounded answer.

### Verification at close

- tests/test_modes.py: **100 tests, 100 passed** (intent deterministic
  matrix + gray-zone/model-tier matrix + router dispatch + qa/research/
  build unit + Docker-gated build e2e + session-loop wiring incl. the
  all-four-modes-one-session test) — plus tests/test_webfetch.py 39/39
  with the glued-URL regression.
- The live Task-F session: 19/19 checks, 4 fetches on the research leg,
  the build leg green through REAL Docker verify against its OWN
  authored tests, the fix leg green through the unchanged loop.
- Known limitation (honest): the gray-zone model tier and the mode
  handlers share the endpoint's health — the reasoning-burn flake is
  retried once everywhere now, but a fully degraded window still fails
  runs honestly (error, never a fake success).

## Plugins & Skills round (2026-09-13) — the skills system (Task A; Tasks B+C are CLI-side, see cli/AGENTS.md)

**Skills: auto-invoked markdown instruction packs, modeled directly on
the SKILL.md pattern this project's own environment uses for its built-in
skills — copied deliberately rather than reinvented.** New module
`harness/skills.py`; the planner now scans available skills' descriptions
before every plan and reads + injects the full SKILL.md of any that
plausibly apply. Skills were on the deferred/stretch list and had never
been built.

### Format + locations (the contract)

- **A skill is a folder containing a `SKILL.md`**: frontmatter `name` +
  `description` (WHEN it applies — the matching signal), body = the
  actual instructions/best-practices. Name falls back to the folder name
  when frontmatter is absent; a frontmatter-only file (no body) is
  skipped.
- **Locations** (all scanned every plan; project wins name collisions —
  the specific beats the general): project `<repo>/.neo/skills/<name>/`
  (committed, team-shared), global `~/.config/neo/skills/<name>/`
  (personal), plugin `~/.config/neo/plugins/<plugin>/skills/<name>/`
  (from installed bundles), plus config `skills_roots` (extra roots;
  tests and the eval pin explicit roots here).
- **Auto-invocation** (not inert storage): `core.run_task`'s planning
  step calls `skills.scan_skills_for_task(...)` — discover, match
  against issue words + retrieval terms + repo path/name segments
  (camelCase-decomposed, task-domain stopwords like fix/bug/test/python
  dropped so generic words never trigger), render, inject as the
  planner prompt's `## Applicable skills` section.

### The cross-module hazard, handled the established way

The section sits **AFTER `## Retrieved context` and BEFORE
`## Constraints`** — the exact placement discipline as decision memory
and coordination fan-out, because T3's difficulty predictor cuts the
planner's first user message at `## Retrieved context`. Skills must
inform planning WITHOUT shifting difficulty scoring. Regression-tested
BOTH ways: placement-order test in tests/test_skills.py + a predictor-
invisibility test (`_issue_text_from` on a skills-carrying prompt —
the scary-words skill body never reaches the issue view).

### Config + trace (all knobs task.config-driven per project convention)

- `skills_enabled` (True — False = the OFF arm, exactly one code path;
  the trace then records `skipped: "skills_enabled=False"` and the
  scan function is never called, test-pinned), `skills_max` (3),
  `skills_max_chars` (2500), `skills_roots` (None = default roots only).
- New trace event `skills` {matched: [names], considered, skipped,
  error, section_chars} — auditable even when nothing applies. Safe to
  surface in dashboards (T4).
- Best-effort BY DESIGN (same contract as decision_memory): missing
  roots/unreadable files/broken scan degrade to "(none matched)" + a
  trace error; planning never dies over a malformed markdown file.

### Matching honesty (same vocabulary as the rest of the stack)

Conservative keyword/camelCase-word overlap — NO embeddings. A skill
with no description still matches on its NAME alone (the brief's
canonical "django-conventions skill for a Django repo" case works even
when the issue never says "django": the repo's own path segments are
task vocabulary — same discipline as decision memory's query building).
Synonym gaps ("average" vs `mean()`) remain future work at the same
embedding seam as retrieval/RECALL.

### Tool-verb extension point (harness/tools.py, feeding Task C)

`extend_batch_verbs(verbs)` lets installed plugins extend the BATCH
read-only allowlist with new READ-ONLY diagnostic commands (e.g.
`ruff check`). Defense-in-depth: verb entries must be plain
word/space/dash tokens, and a FIRST-TOKEN deny set (`rm, sed, python,
git, curl, docker, sudo, ...` — see `_DENY_VERB_TOKENS`) makes a
hostile or malformed manifest entry structurally unable to whitelist
destructive or arbitrary-execution commands. `_batch_readonly_pattern()`
merges extensions INSIDE the base pattern's non-capturing group (an
extended verb anchors exactly like a built-in); the forbidden-
composition guard (pipes/redirects/&&/$()) still applies verbatim.
Regression-pinned: `validate_batch(["rm -rf /"])` rejects even when
"rm -rf" was fed as a plugin verb.

### Verification at close (all real, not assumed)

- tests/test_skills.py: **27/27** — parsing (frontmatter/fallbacks/
  malformed/huge-body cap), discovery (precedence, dot-dirs, missing
  roots), matching (django applies to django tasks; pandas does NOT;
  irrelevant skills ignored; repo-name-only match; max bounds), prompt
  placement + predictor invisibility, config defaults, and **three
  REAL-loop e2e** (scripted model through real run_task): content-
  receipt (a matching skill's unique body marker demonstrably arrives
  in the planner's OWN user message), irrelevant-skill non-injection
  (the marker must NOT appear), and OFF-arm (factory-call counter = 0
  when skills_enabled=False).
- Live plugin-chain e2e (driver at Temp/opencode/plugin_e2e.py): install
  the example plugin → apply_tool_extensions → run a real fix on a
  django-shaped fixture → the plugin's skill body marker IS in the
  planner prompt + skills trace event shows the plugin-origin match →
  verified loop SUCCESS.
- **Full eval matrix 14 tasks × 8 arms: 112/112 CLEAN, 0 regressions**
  (the standard pre-ship gate — this round changed the planner prompt).
  New scenario `eval_skills_injection` (genuinely-matching pytest-
  conventions skill via config skills_roots; the scripted fix never
  depends on skill content, so both arms stay deterministic) + new
  `no_skills` arm + `skills_enabled` in `_ROUND_KEYS`. Task set 13→14,
  arms 7→8 — prior reports stay comparable per-task.
- Regression sweep: skills 27 + CLI plugin/command suites 30 + the
  harness/CLI/eval selections — **515 green** across the round's sweep
  (e2e_run_task, decision_memory, coordination, agent_tests,
  self_critique, batch_docs_lint, evals_tasks, adversarial, cli*).
- ruff: all NEW files violation-free; the ratchet shows NO new debt on
  any file this round touched (core.py's count went DOWN 3→2; the
  ratchet's red files are parallel sessions' in-flight work, not this
  round's).

### Example skills shipped (real, not toys)

`tests/fixtures/skills/` — `pytest-conventions` (suite invocation,
node ids, fix-code-not-tests), `django-conventions` (migrations, ORM,
serializers, N+1), `pandas-vectorization` (vectorize loops, NaN
semantics, index alignment, chained assignment). The eval's skills
scenario pins this same root via skills_roots.

## Web-page reading round (2026-09-13) — the FETCH tool (Tasks A+B+C)

**Generalizes and supersedes the narrower docs-lookup scope: a proper,
general-purpose web-fetch capability usable during planning or repair —
not just library docs.** New module `harness/webfetch.py` + a `FETCH
<url>` step-session control signal, wired exactly like RECALL/BATCH/
DOCS (parsed raw AND fence-stripped, never executed as shell).

### Task A — the fetch_webpage tool (harness/webfetch.py, stdlib-only)

- `FETCH <full url>` in place of a bash command → the harness GETs the
  page, extracts readable text, re-injects into the live session.
- **Readability-style extraction** (deliberately simple, per brief):
  a stdlib `HTMLParser` subclass drops script/style/noscript/template/
  svg/iframe/form/nav/header/footer/aside WITH their content; block
  tags give line structure; whitespace/blank-lines collapse; entities
  decode. **Semantic-container preference**: text inside
  `<main>`/`<article>`/PyPI-style `div.project-description` is captured
  per container and the INNERMOST non-empty container wins over the
  whole body (PyPI: the description div beats its `<main>` wrapper, so
  the sidebar/nav never reaches context). Malformed markup tolerated
  (half-parsed text still renders). A JS-only page honestly returns
  `no_text`.
- `parse_fetch` requires an explicit `http(s)://` scheme — `FETCH_ME`
  identifiers never false-trigger (bare words stay bash commands).

### Task B — scope & safety (read-only by construction)

- **GET only**: `urllib.request.Request(method="GET")`, UA declared, no
  body/headers/forms/auth/POST — impossible by construction, not by
  policy.
- **SSRF guard** (the fetch runs on the HOST, outside the sandbox):
  scheme allowlist (http/https) + host blocklist (localhost/loopback/
  Link-Local/Unique-Local/Reserved/Unspecified/multicast IPv4+IPv6
  literals, `0.0.0.0`) — a blocked URL fails CLOSED with a reason slug
  and NEVER opens a socket (regression-tested with urlopen monkeypatched
  to explode). Redirects re-validated at EVERY hop, capped (default 3,
  hard ceil 5), loop-detection via a seen-set.
- **Bounds**: `webfetch_timeout_s` (15, floor 3) bounds the whole fetch;
  `webfetch_max_bytes` (1 MiB, floor 64 KiB) enforced DURING the read
  loop (a huge page can never blow memory, let alone context);
  `webfetch_max_chars` (3000) caps the rendered text with a truncation
  marker; a Content-Type gate refuses non-text/html bodies.
- **Auditability**: every fetch logs a `web_fetch` trace event
  {step_id, turn, url, ok, status, chars} — URL + timestamp + outcome,
  same discipline as any tool call — AND emits to the unified
  cross-module stream (`shared.tracing`, module="harness", opt-in env).
- **Budget**: `max_fetches_per_step` (3) with an exhaustion nudge that
  cannot deadlock (same pattern as RECALL/DOCS); `web_fetch_enabled`
  (True) is the OFF arm (single code path, config-only — the eval's
  `no_webfetch` arm and `pre_round` toggle it).

### Task C — the real case, end-to-end (tests/test_webfetch.py)

**Fixture `tests/fixtures/bug07_num2words`** (new): `year_phrase(2023)`
returns "two thousand and twenty-three" instead of "twenty twenty-three"
— the fix is num2words' dedicated `to="year"` converter, and the
information gap is GENUINE by construction: num2words is NOT installed
on the host (pydoc/DOCS genuinely miss — regression-tested:
`test_docs_genuinely_misses_num2words`), the kwarg is not in the repo,
and a plausible wrong guess (`year=True`) raises TypeError (the honest
control: attempt 1 with the wrong kwarg genuinely FAILS, proving the
API knowledge gap is real — `test_task_c_fixture_genuinely_fails_...`).

**The proof** (`test_task_c_fetch_docs_fixes_unfamiliar_library`,
Docker+network gated): the scripted model FETCHes
`https://pypi.org/project/num2words/` mid-step; the model REFUSES to
apply the fix until `"to: The converter to use"` demonstrably arrives
in ITS OWN message list (content-receipt gate); the applied fix uses
`to="year"`; verified success through the REAL loop + REAL Docker
sandbox/verify; trace shows the `web_fetch` event (url/ok/status) and
NO tool_call ever executed the FETCH line as shell. The honest control
runs the same fixture with FETCH disabled: the wrong guess burns an
attempt before the right kwarg passes — without the web, attempt 1 is
the only shape a guessing model has.

**Live extraction pinned**: `test_live_fetch_pypi_num2words` asserts the
real PyPI page's `to=` converter list arrives, site chrome ("Skip to
main content") does not, and the text is capped.

### Wiring (all additive; no boundary changes)

- `core.run_step`: FETCH intercept after the DOCS block (raw +
  fence-stripped parse), budget counters, trace + unified-stream audit
  closure via `fetch_and_render(audit_hook=...)` (webfetch module stays
  trace-plumbing-free; the loop owns logging).
- `prompts.py`: `## Reading a web page (FETCH)` block in the STEP system
  prompt (planner prompt + difficulty markers untouched — T3's
  predictor cut is safe by the same placement discipline as BATCH/DOCS).
- `config.py`: web_fetch_enabled, max_fetches_per_step,
  webfetch_timeout_s, webfetch_max_bytes, webfetch_max_chars,
  webfetch_max_redirects.
- **evals**: new arm `no_webfetch` + `web_fetch_enabled` in `_ROUND_KEYS`
  (arm-key sync test-pinned); new scenario task `eval_fetch_webpage`
  (13 tasks total now) — the scripted model FETCHes the real PyPI page,
  then fixes a string-to-int bug whose correctness does not depend on
  the fetched CONTENT (determinism preserved on any fetch outcome; the
  scenario guards loop machinery, like eval_docs_lookup).
- New trace events: `web_fetch` (safe to surface in dashboards).

### Verification at close (all real, not assumed)

- tests/test_webfetch.py: **38/38** (35 offline incl. SSRF matrix,
  extraction, bounds, loop wiring e2e via monkeypatched fetcher; 3
  Docker+net gated: the Task C proof, the honest control, DOCS-miss).
- Full eval matrix **13 tasks × 7 arms: 91/91 ok, CLEAN, 0
  regressions** (the standard pre-ship gate for the step-prompt change).
- `python -m evals.run --check`: 13/13 OK (incl. eval_fetch_webpage).
- Harness selection (config_trace_state, stubs_and_deps,
  retrieval_tools, editor_prompts, recall_unit, adversarial,
  tool_errors, batch_docs_lint, webfetch, evals_tasks): **225/225**;
  e2e_run_task **28/28**; coordination/decision_memory/agent_tests/
  self_critique/state_machine **95/95**.
- ruff: new files violation-free; lint ratchet: no new debt from this
  round (the listed failures are other terminals' pre-existing debt).

### Known limitations (honest)

- The readability extractor is deliberately simple (container-preference
  + boilerplate dropping, NOT a scoring algorithm) — pages without
  semantic markup return their whole-body text minus nav/script/footer.
- The unified-tracing emit imports `shared.tracing` lazily inside the
  audit closure (opt-in env; a failure is swallowed — observability
  must never change task outcomes).
- A parallel session's bulk commit `41423dc` swept this round's files
  in mid-flight (verified intact by the green suites above); my
  post-commit lint fixes are the remaining working-tree delta.


## Improvement Round 2, second session (2026-09-11/12) — Agent-written edge-case tests (Tasks A+B+C) + self-critique restoration

(Session interrupted twice: the original terminal closed mid-round —
resumed by forensically reconstructing state from the tree, the empty
`logs/agent-tests-ablation/` dir (created 14:30, never populated), and
the recovery-stash tags; a second interruption closed the terminal during
the fixtures ablation, which continued detached and completed.)

### Pre-flight: a blocked dependency found and fixed first

`tests/test_self_critique.py` failed at COLLECTION — the Agent
Intelligence round's self-critique code (`render_self_critique_prompt`,
`_self_critique`, config keys, Boundary-7 `structured_feedback` on
VerificationResult) was destroyed by the documented git-hook incident
(INTERFACES.md's recovery note: "harness/retrieval.py,
shared/types.py (structured_feedback) and execution/verify.py Round-8
deltas were NOT recoverable from git — T1/T2 please re-write"; the
03:23 snapshot survived only under stash tag `recovery-stash-t1-agent-
intel` with the warning that T1's in-flight re-writes landed after).
This round BUILDS ON that work per its brief, so it was restored FIRST,
from the stash + the surviving tests as the spec:

- `harness/prompts.py`: `SELF_CRITIQUE_SYSTEM` + `render_self_critique_prompt`
- `harness/core.py`: `_self_critique` + the success-path gate (after
  final verify passes, ONE review call of the diff vs the ORIGINAL
  issue; "no" verdict poisons the attempt with the reason as feedback;
  unparseable reply = approve — critique never kills a verified fix on
  its own parse failure) + `_structured_or_tail` feeding
  `_target_feedback`/`_regression_feedback` (Boundary-7 objects when
  present, raw tail otherwise)
- `harness/config.py`: `self_critique` (True), `self_critique_max_chars`
  (8000)
- `shared/types.py`: `structured_feedback: List[Dict] = field(
  default_factory=list)` (additive, default-constructed — every existing
  call site stays signature-valid); `runtime/serialize.py` (T3's file,
  flagged in the Change Log) round-trips it with an absent-key default
  so old journals replay
- 13/13 tests/test_self_critique.py green post-restore (Docker-gated
  e2e included).

### Task A — agent-written edge-case tests (harness/agent_tests.py + prompts + wiring)

The gap: success was gated on the ONE given failing test + full-suite
regression. A fix can satisfy exactly the given test while being wrong
for cases the issue clearly implies (boundaries, error conditions,
adjacent inputs). The gate: after final verify passes but BEFORE
success is minted, ONE model call (`render_agent_tests_prompt` carries
issue + candidate diff + a listing of the repo's existing test files)
returns JSON `{"tests": [{filename, content}]}`. Strict sanitize
(bare `*.py` names only — charset `[A-Za-z0-9._-]`, no path/drive
tricks, dupes dropped, content compiled as Python, config-capped file
count + total chars); anything failing sanitize is DROPPED, never
fatal. Config: `agent_tests` (True), `agent_tests_max` (3),
`agent_tests_dir` ("tests/_agent_generated"), `agent_tests_max_chars`
(12000).

### Task B — same verification rigor, no lighter path

Surviving tests run through the SAME `verify()` everything else uses,
in two stages, on TRANSIENT trees under `logs/{task_id}/agent_tests/`
(never inside work/ — no cleanup can leak into a diff):
1. **Baseline**: `verify(pristine+tests, rerun_for_flake_check=0)` —
   exactly the task-baseline convention. A generated test that PASSES
   pre-fix probes nothing the final gate didn't cover; dropped with a
   trace event.
2. **Post-fix**: `verify(work+tests, rerun_for_flake_check=baseline_reruns)`
   — the generated file is the TARGET: flake-rerun semantics + the
   full-suite regression, identical rigor to the final gate.

Policy (symmetric with the lint gate): a post-fix FAILURE poisons the
attempt (retry with the failing test's output as feedback — structured
Boundary-7 objects when present); a GENERATION problem (model crash,
unparseable reply, zero survivors, copy/verify crash) SKIPS the gate —
never overturn a verified fix over test-writing quality; every skip is
trace-logged. Surviving tests saved to `agent_tests/saved/` for human
review regardless of outcome. Gate order in run_task: edit-validation →
lint → final verify → agent-tests → self-critique → success.

**18/18 tests/test_agent_tests.py green** (parse/sanitize matrix +
Docker-gated e2e: gate-pass with saved-test + work/-hygiene asserts,
gate-poison→retry→pass, unparseable-skip, all-baseline-pass skip,
disabled-config, poison-exhaustion→failed). Full module selection at
close: **181/181** (150 prior + 13 restored self-critique + 18 new),
~6 min warm Docker.

### Task C — the ablation, run honestly (three runs, one real)

`harness/ablation_agenttests.py` (mirrors ablation_memplan's design:
real scheduler → real workers → real run_task → real Docker verify;
model PINNED to glm-5.3 for every call, routing OFF, `self_critique`
pinned OFF in both arms so the agent-tests gate is the only delta;
gate-specific metrics mined from traces: generated/fires/passes/skips).

- **multirepo run (logs/agent-tests-ablation/multirepo)**: DEGENERATE —
  0/5 success BOTH arms. The endpoint's planner calls ran 239–797s and
  every task wallclock-died mid-plan (10 timeouts); the gate never
  reached. Measures endpoint latency, not the mechanism; kept as run-
  condition evidence, quoted as such.
- **fixtures run 1 (fixtures-broken-cap/)**: found a REAL failure mode
  first: the `max_completion_tokens=4000` mitigation (added for the
  documented reasoning-burn hazard) made the generation call return
  EMPTY 5/5 (`raw: ""`, finish_reason=length, 4000 hidden reasoning
  tokens, 0 visible) — probe-verified directly against litellm
  (probe_logs/atgate-mct-*.json): uncapped = 29k hidden tokens then
  content; capped-4000 = empty. The gate's skip policy held (5/5 clean
  skips, zero false poisons) — the harness did its job on bad model
  output. Mitigation REMOVED; wallclock raised to 2400s.
- **fixtures run 2 (logs/agent-tests-ablation/fixtures/, the real one)**:

| arm | success | attempts | calls | tokens | cost | wall |
|---|---|---|---|---|---|---|
| OFF (gate off) | 5/5 100% | 5 | 20 | 48,364 | $0.0557 | 524s |
| ON (gate on) | 3/5 60% | 7 | 33 | 79,941 | $0.1096 | 5677s |

Gate engagement (ON): 8 tests generated across 5 tasks, 3 gates ran to
completion (bug02: 2 tests generated → both survived baseline → both
passed post-fix, gate-pass; bug03/bug05 similar), **0 gate FIRES** (no
fix passed the whole suite while failing an issue-implied edge), 0
skips. The 2 ON-arm failures (bug01, bug04) are wallclock TIMEOUTS
during the gate's generation call — traces show final verify PASSED,
then the ~300s+ generation call blew the 2400s budget (these fixtures'
fixes were verified-good; the success just never got minted before the
kill; resumed once and timed out again in the same place — the
documented slow-endpoint window, 150–345s/call measured in the probes).

**Honest verdict: on this task set the gate did NOT measurably improve
fix quality — 0 fires in 3 completed gates; its measured effect was
purely cost (+$0.054, +65% calls, +3.9x wall on completed tasks) plus
2 successes LOST to the generation call's latency inside a finite
wallclock.** The counter-evidence to "useless": (a) bug02's generated
tests genuinely probed issue-implied edges the 5-test suite didn't
cover (single-element/negative/float mixes) and the correct fix passed
them — a wrong-but-suite-green fix would have been caught (the e2e
poison test proves that path works end-to-end); (b) the 5 fixture
repos have TINY suites (2-5 tests) whose given tests already encode the
edges — the class the gate catches needs a suite whose given test
under-constrains the issue (the multirepo set, where the suite pins
are SUBSET pins — exactly where the gate has room to fire, and exactly
where this endpoint window could not complete a run). Where it would
pay: repos with sparse suites + fast endpoints; with a fast/cheap
generation model (the call is one-shot, no conversation) the latency
cost mostly vanishes. Recommendation recorded: keep the gate ON by
default (cheap insurance when the endpoint is healthy; correct skip
behavior when it isn't), pin `max_wallclock_s` ≥ 2x the observed p95
model call when the gate is on, and re-run the multirepo arm in a
faster endpoint window before making any keep/drop call at scale.

Standing honesty notes: 5 tasks x 1 rep; proxy prices (free-tier
endpoint); the two endpoint-degraded runs are evidence about the
ENDPOINT, not the mechanism; n too small for any success-rate claim in
either direction.

### Files this session

- `harness/agent_tests.py` (NEW), `harness/ablation_agenttests.py` (NEW)
- `harness/core.py` (gate wiring + restored critique + _structured_or_tail),
  `harness/prompts.py` (AGENT_TESTS prompts + restored critique prompt),
  `harness/config.py` (agent_tests* keys + restored critique keys)
- `shared/types.py` (structured_feedback restored), `runtime/serialize.py`
  (round-trip, T3's file, flagged), `INTERFACES.md` (Change Log entry)
- `tests/test_agent_tests.py` (18, was in-tree untracked — verified),
  `tests/test_self_critique.py` (13, passes post-restore)
- probe_logs/atgate-* (endpoint probes: the 29k-hidden-token measurement,
  the cap-vs-empty proof, run logs)

## Interactive-mode fix (2026-09-12, visiting CLI session) — editor.snapshot dst-inside-src RecursionError

**Found live by driving the real `neo` no-args interactive session in
a scratch repo** (the Task-D verification round; full story in
cli/AGENTS.md): `cd <repo>; neo` defaults the log root to ./logs
INSIDE the target repo, so `editor.snapshot(repo, logs/{task_id}/
pristine)` had dst-inside-src and `shutil.copytree` recursed into its
own output until RecursionError → task "error" before the first model
call. Every scripted caller placed logs outside the repo, so the shape
was never exercised.

**Fix (this module's editor.py::snapshot only)**: when dst's parent
chain runs through src, the top chain segment (e.g. `logs`) is
excluded from the copy via the ignore hook. Signature unchanged;
log-root-outside-repo behavior byte-identical (a `logs/` dir that is
real repo content still copies — pinned). Harness artifacts never
belong in the pristine reference anyway, so the exclusion is also
semantically right. 4 regression tests in tests/test_editor_prompts.py
(inside-repo root, outside-root no-over-exclusion, dst-directly-under-
src, plain shape) — file now 19/19; full sweep after the fix: 340
green across the harness + CLI selections (e2e_run_task 28, adversarial
43, coordination 31+5, config_trace_state 12, stubs/retrieval/recall
51, editor_prompts 19, CLI suites 125, coordination_e2e/decision-
memory/env-snapshot 32).

**Also fixed in passing (NOT this module's author, flagged per
cross-terminal practice)**: `harness/ablation_agenttests.py` — a
parallel session's in-flight untracked file — had a PowerShell UTF-8
BOM that tripped test_harness_modules_import_and_parse; stripped the
3 BOM bytes, content untouched (that file is 12/12 after).

## Round 8 (2026-09-10) — agent execution layer & tooling (Tasks A-D)

**54 new tests (all green): tests/test_tool_errors.py (26) +
tests/test_batch_docs_lint.py (28). Full-module selection at close:
204 passed (150 prior + 54 new), ~9 min warm Docker, ~4 min Docker-less
(the new suites need NO Docker).**

### The incident this round survived (read before touching harness files)

At ~09:37 a PARALLEL session ran `git reset --hard HEAD~1` to revert its
own smoke-test commit — which also destroyed every UNCOMMITTED harness
change in the tree: the documented Round-6 adversarial hardening
(tools.py deny patterns, core.py final-edit re-validation, editor.py
traversal normalization), my in-flight Round-8 wiring, and a second
session's in-flight state-machine/self-critique/rerank work. Untracked
files (all new modules + test files) survived. I recovered the Round-6
hardening from dangling stash-commit `fa75dcb` ("round7-verify-
committed-state"), re-applied my Round-8 work on top, and re-verified
(test_adversarial 43/43 green again). tools.py was clobbered twice more
by background git operations mid-round; the recovery pattern that worked:
immediate post-write backups OUTSIDE the repo (Temp/opencode) + import-
verification before each test run. **Lesson for all terminals: COMMIT
(or at minimum back up outside the tree) after every verified-green
milestone — the working tree is shared by 4 sessions and is not safe
storage.** The other session's state-machine/self-critique/rerank/
decision-memory work re-landed from their own session and now coexists
with mine in core.py/context.py/config.py.

### Task A — structured tool-call error handling (harness/tool_errors.py)

A failing/malformed tool call used to feed the model raw exit codes +
stderr dialect noise (or a raised traceback). Now every failure is
classified into a short, actionable error type — `TOOL ERROR [<kind>]:
<detail>` + a one-line `Suggested fix:` — so the model gets a signal to
act on instead of noise to parse. The raw output still rides the result
(capped) for diagnosis; classify() NEVER raises (degrades to
internal_error with the original preserved).

**10 error classes** (26 tests; the requirement was 5):
file_not_found, command_not_found, malformed_patch (with "hunk doesn't
apply at line N"), syntax_error, import_error, undefined_name,
permission_denied (incl. the protected-path shape), timeout,
argument_error, command_rejected (the deny-guard PermissionError), +
internal_error fallback + "ok" no-op so callers can classify
unconditionally.

Wired at three points:
1. `BashSession._map_result` — every nonzero/timeout result gets the
   classification prefixed; exit=0 renders EXACTLY as before (the
   "exit=0" string consumers are unaffected).
2. `BashSession.run` — sandbox-layer exceptions (other than
   PermissionError/SandboxUnavailableError, which keep their own
   classes — fail-loud is unchanged) are classified and re-raised as
   `ToolExecutionError(kind, detail)`.
3. `core.run_step` — catches ToolExecutionError and feeds structured
   feedback (`tool_error` trace event) instead of a traceback.

Two real classifier bugs were found by its own tests and fixed:
patch "at line N" reported the HUNK number not the line; python
SyntaxError line numbers live on the preceding `File "x", line N` line.

### Task B — batched read-only execution (BATCH protocol)

`BATCH <cmd> ;;; <cmd>` — several independent READ-ONLY commands in one
turn instead of one per turn. Deliberately NARROW (per the brief: no
general parallel execution):
- Strict verb allowlist (cat/head/tail/ls/dir/find/grep/rg/wc/file/stat/
  pwd/which/where/env/git status|diff|log|show|blame|ls-files/
  python -m pydoc) + FORBIDDEN composition chars (< > | & ; ` $( and
  newlines). One bad entry rejects the WHOLE batch (all-or-nothing —
  no partial semantics); the rejection names the entry.
- `python -c` is deliberately NOT allowlisted (executes arbitrary code;
  cannot be verified read-only); `python -m pydoc` IS (renders docs only).
- Entries run via ThreadPoolExecutor(4) through the SAME BashSession
  path (deny guard, capping) with throwaway sessions (order-independent:
  no shared cwd coupling). Fenced batches are intercepted on raw AND
  fence-stripped forms (never executed as one shell line — regression-
  tested, same discipline as RECALL).
- Trace: one `batch_call` event (+ per-entry tool_call/tool_result
  flagged `batch: true`, plus `batch_rejected` on refusals).

**Measured wall-clock saving (real Docker sandbox, 8 read-only ops —
the multi-file diagnosis shape)**: read phase 12.62s serial → 6.41s
batched (**1.97× faster, 6.22s saved**; bounded by 4 workers on 8
ops). Full task incl. identical verify overhead: 25.19s → 18.84s
(1.34×). Plus 8 model round-trips collapsed to 2 — with a real model
the saving is strictly larger. Scripts: measure_batch.py (full task)
+ measure_batch_phase.py (phase-isolated); regression-encoding:
test_run_batch_concurrent_wallclock_speedup asserts the concurrency
win in CI (no Docker needed).

### Task C — lint/static-analysis gate (harness/lint.py)

Stdlib-only (no new deps; works offline, every CI cell): per-changed-
file `compile()` syntax check + a conservative single-file undefined-
name pass (module-level uses vs every definition anywhere in the file —
imports incl. star, def/class at any nesting, comprehension targets,
global/nonlocal, walrus, except-as, `__all__` re-exports, builtins).
FALSE-NEGATIVE biased by design (function bodies not analyzed — local
control flow makes single-file analysis false-positive-prone, and the
cost model is asymmetric: a missed error costs one verify which still
catches it; a false positive would block a verifiably-correct fix).
Verified clean against all 5 fixtures + every scripted e2e fix shape.

Wired at TWO points in the loop controller (config: `lint_gate` True,
`lint_names` True):
1. **SUBMIT-time, in-session** (run_step): findings feed back into the
   SAME session (model is mid-context — cheapest fix loop; turn budget
   bounds it). Never ends the step, never gates success.
2. **Pre-final-verify short-circuit** (run_task): between the Round-6
   edit-policy gate and the final verify — a lint failure poisons the
   attempt (retry with findings as feedback) BEFORE burning a sandboxed
   pytest cycle on an edit the AST pass already knew was broken.
   Trace-proven ordering in the regression test: lint_failed →
   attempt_start with NO final_verify between.
Verifier-gated completion stays absolute (spec item 17): lint only
short-circuits what it KNOWS is broken; `lint_failed` trace events
carry the classified findings.

### Task D — documentation/API lookup (DOCS protocol)

`DOCS <dotted target> [topic words]` — same control-signal contract as
RECALL (parsed raw + fence-stripped, never executed as shell). Read-
only by construction; the ONLY remote call is the fixed PyPI JSON
endpoint, GET-only, gated OFF by default (`docs_lookup_allow_remote`
False — network stays opt-in per task).

Resolution layers: shared cache (`logs/_docs-cache/`, same convention
as `_code-graph/` — harness-owned, outside the repo, atomic tmp+replace
writes) → pydoc of the installed interpreter in a SUBPROCESS (never
import agent-adjacent code in-process; a subprocess dies cleanly) →
opt-in PyPI metadata. Caps everywhere (3000 chars default, 10s
timeout). Budget: `max_docs_per_step` (3) with an exhaustion nudge
that can't deadlock (same pattern as RECALL). New trace event
`docs_lookup` {query, source, ok}; pydoc-miss detection is case-
insensitive (a real bug found by its own test).

### Files this round

| File | Role |
|---|---|
| `tool_errors.py` (NEW) | classify()/classify_exception()/render_error(): 10 stable error kinds + hints; never raises |
| `lint.py` (NEW) | lint_file/lint_changed/render_findings: syntax + module-level undefined names, stdlib-only |
| `docs_lookup.py` (NEW) | parse_docs/lookup/lookup_and_render: cache → pydoc subprocess → opt-in PyPI |
| `tools.py` | + parse_docs/parse_batch/validate_batch/run_batch; _map_result classifies failures; run() wraps sandbox exceptions as ToolExecutionError; SandboxUnavailableError still fail-loud |
| `core.py` | + BATCH/DOCS intercepts in run_step (raw + fence-stripped), SUBMIT-time lint gate, pre-final-verify lint short-circuit, tool_error handling |
| `prompts.py` | + BATCH and DOCS doc blocks in the STEP system prompt (planner prompt markers untouched) |
| `config.py` | + lint_gate, lint_names, docs_lookup_enabled, docs_lookup_allow_remote, max_docs_per_step, docs_max_chars |

### Notes for other terminals

- **T3 (runtime)**: state.json schema unchanged; new trace events
  (batch_call, batch_rejected, tool_error, lint_failed, docs_lookup)
  are additive and safe to surface. `cfg["_docs_cache_root"]` is a
  private harness key (underscore prefix) — don't rely on it.
- **T2 (execution)**: nothing needed; all new machinery rides the
  existing execute_sandboxed contract.
- **T4 (memory)**: `logs/_docs-cache/` is shared/harness-owned like
  `_code-graph/` — harmless to prune (rebuilds lazily).
- tests/test_e2e_run_task.py `_assert_logs_complete` now asserts the
  6 Boundary-4 keys as a PREFIX (additive keys allowed) — updated for
  the parallel session's repo_path state key per the INTERFACES.md
  consumer note.

## Improvement Round 2 — memory-informed planning (2026-09-10, T1+T4 joint)

(Owns: harness/prompts.py planner step + the memory module's query
interface. Two OTHER parallel sessions were actively editing this tree
during the round — Round-8 BATCH/DOCS/lint/agent-tests and the
coordinated-changes round below; this round's edits were confined to
prompts.py (planner prompt), core.py (planner wiring), config.py,
context.py (additive state key), deps.py, decision_memory.py (new), and
the memory module's decision_store.py — no overlap with their sections
of prompts.py/core.py beyond additive coexistence, verified by their
suites staying green.)

### Task A — the planner now actively queries decision memory

**The gap was real**: the memory layer stored decisions and was
queryable (MCP `query_decisions`), but the PLANNING STEP never consumed
it — decisions landed in the store only after tasks finished. This was
explicitly listed in "What's next (Phase 3 hooks)" as unstarted.

The wiring, end to end:
- **New module `harness/decision_memory.py`**: `query_planning_decisions
  (repo_path, issue_text, retrieval_terms, limit)` — builds a keyword
  query from the issue's retrieval terms PLUS the repo's own path/name
  segments (convention rows mention the package name; the issue often
  doesn't), calls `DecisionStore.search(query, limit, repo_path=...)`
  scoped to THIS repo, renders text+origin lines. `render_memory_block`
  caps the section (`memory_max_chars`, 1500) with a truncation marker;
  empty renders as an explicit "(none recorded yet)" so the model knows
  memory was CONSULTED and had nothing. Never raises: missing memory
  module / unopenable store / broken query → empty + error string
  (planning must not die because memory is down).
- **`harness/deps.py::get_decision_store_factory`**: resolves
  `memory.decision_store.open_default_store` real-first, None on
  ImportError — same pattern as the code-graph factory. (New T4 surface,
  see INTERFACES.md 2026-09-10.)
- **Planner prompt** (prompts.py): new `## Relevant past decisions`
  section, deliberately placed AFTER `## Retrieved context` and BEFORE
  `## Constraints` — T3's difficulty predictor cuts the first user
  message at `## Retrieved context`, so decision memory can never shift
  difficulty scoring / routing (the placement is regression-tested from
  the runtime side: `test_memory_section_invisible_to_difficulty_
  predictor`). The predictor cut marker itself is untouched.
- **core.py planning step**: after retrieval, before the planner call —
  query (when `plan_with_memory`, default True), render, inject as
  `memory_block=`; new `decision_memory` trace event {query, matched,
  error, section_chars} or {skipped: "plan_with_memory=False"}. OFF is
  exactly one code path (config-only) — the ablation depends on that.
- **Config keys** (harness/config.py): `plan_with_memory` (True),
  `memory_query_limit` (6), `memory_max_chars` (1500).
- **state.json repo_path** (context.py, ADDITIVE): TaskState now records
  the task's repo_path after the six Boundary 4 keys (omitted when
  empty). T4's `ingest_state_file` has read `data.get("repo_path")`
  since Round 1 — the reader predated the writer; now ingestion stamps
  rows per repo so the repo-scoped query has something to match. The
  six-key prefix order is unchanged and still asserted
  (STATE_KEYS_WITH_REPO added; existing schema test untouched and still
  green). Boundary 4 note updated in INTERFACES.md.
- **Tests**: tests/test_decision_memory_planning.py (17): memory-side
  repo filter + normalization + open_default_store location; harness-
  side query building/caps/degradation (duck-typed store, monkeypatched
  factory); prompt placement contract + predictor invisibility; state
  repo_path prefix-order + ingest round-trip; and three REAL-loop e2e
  (scripted model) proving content-receipt — the planner's actual user
  message contains a store marker unseen by the issue text (ON),
  OFF-arm never opens the store (factory-call counter = 0), and a
  broken store degrades to "(none recorded yet)" + trace error without
  killing the task.

**Plumbing pilot (deterministic, before spending model budget)**: real
scheduler → real worker subprocesses → real harness → real Docker
verify, scripted model — ON arm: 5/5 tasks success with seeded
repo-scoped decisions demonstrably in the planner prompt (matched 1-2
each), the no-repo global row never leaks into any prompt; OFF arm: 5/5
success, no memory rows, skip events. logs/memplan-pilot/run-*/.

### Task B — the ablation: does it actually help? (honest answer)

**Method** (runtime/ablation_memplan.py, mirroring runtime/ablation.py
conventions): 5 fixture tasks, REAL full stack both arms (scheduler →
worker subprocesses → run_task → Docker sandbox/verify), SAME pinned
model for every call (z-ai/glm-5.3-free via tokenrouter, adaptive
routing OFF — a routing confound would make the delta unattributable),
arms differ in exactly `plan_with_memory`. The precondition — relevant
prior decisions exist — satisfied by SEEDED rows genuinely mined from
prior-run traces (v4/v3: the bare-`pytest` ImportError class that
burned real turns in abl-off-bug01's attempt 1, sed-with-quotes
breakage from the bug03 traces, per-repo suite-invocation conventions,
the successful strategies prior fixes actually used), recorded via
`DecisionStore.record` (source "manual", repo_path stamped) — the
documented path for facts not in state files. Dedicated ablation DB
(HARNESS_DECISIONS_DB pinned per-run); production store untouched.
Proxy prices per the standing honesty notes.

**Result (run1, logs/memplan-ablations/run1/summary.json):**

| metric | OFF | ON | delta |
|---|---|---|---|
| success | 5/5 | 5/5 | none |
| attempts (all tasks) | 1 | 1 | none |
| verify failures | 0 | 0 | none |
| harness-level model calls (trace) | 35 | 27 | **-23%** |
| tokens | 147,916 | 112,390 | -24% |
| proxy cost | $0.2148 | $0.1782 | -17% |
| known-mistake recurrences | **3 tasks** | **0 tasks** | -3 |

**What actually changed**: the three OFF-arm recurrences are all the
SEEDED known-mistake class — bare `pytest` → ImportError → wasted
diagnostic turns (bug01, bug02, bug03). In the ON arm ZERO tasks
re-tripped it: first commands were `python -m pytest` from the repo
root, matching the seeded convention. And the causality is visible in
the plans/commands, not just aggregates: ON-bug03's plan used the
python-rewrite strategy (its seed row documents sed-quote breakage from
the v3 traces); ON-bug05's plan repeats the module-level-list
diagnosis; ON-bug01 used the pathlib exact-block replacement its seed
row describes.

**What did NOT change — the honest core**: success rate, attempts, and
verify failures are IDENTICAL. The fixtures are easy for this model
class either way; memory did not add task-solving power, it removed
wasted work. The "avoid repeating a previously-discovered mistake"
question: YES, measurably (3→0). The "fewer attempts" question: NO at
this task difficulty (nothing needed a second attempt). Anyone quoting
this round should say "efficiency + mistake-avoidance at equal
success", not "memory makes the agent better".

**Honest caveats (all in the run's summary.json post_run_analysis)**:
n=5×1rep directional only; the per-arm "ledger calls" metric is
INFLATED by router-internal empty-response retries (ON-bug01: 5
sub-second compl=0 entries inside 4 real calls — I recount trace
model_request events for the honest number); each arm absorbed exactly
one symmetric mid-run crash-retry (OFF-bug05, ON-bug02 — runtime
resumed both; duplicated post-resume calls included in counts); a
parallel terminal's in-flight agent-tests feature ran symmetrically in
both arms; the seeded rows are convention/gotcha facts mined from real
traces, not fix cheat-sheets (no row says "change X to Y").

### Verification at close

New suite 17/17; harness selection (config_trace_state,
stubs_and_deps, retrieval_tools, editor_prompts, recall_unit,
adversarial) 122/122; e2e_run_task + decision_memory_planning 45/45;
memory-module suites (decision_store, code_graph, mcp_server,
mcp_client, mcp_stdio_fileno, dashboard) 56/56; cli + cli/mcp
adversarial 117/117. All this round's edits coexist with the two
parallel sessions' in-flight rounds (their suites re-run green in the
same sessions; the shared-file coexistence verified by the combined
green runs above).


## Improvement Round 2 — coordinated multi-file changes (2026-09-10, parallel session)

(Round 8 was landing in-flight in this same tree during this round —
lint gate, BATCH, DOCS, state machine, decision memory, agent-tests.
This round built additively ON TOP of that state; the pre-flight
baseline of the 8 landed suites was verified green FIRST: 166/166,
~5:35 warm Docker. Two in-flight test files — test_context_budget_
rerank.py, test_self_critique.py — did not collect at round start
(their harness halves hadn't landed yet in my reads; they belong to
the parallel session and were left alone.)

### Task A — detect when a fix genuinely requires coordinated changes

**New module `harness/coordination.py`.** Two cooperating pieces:

1. **Planning-side fan-out** (`detect_coordinated_change`): before the
   planner runs, the structural graph (Terminal 4's memory.code_graph,
   raw Graph like retrieval consumes — no new memory surface) is
   walked from the files retrieval ranked: every CALLER of a symbol
   defined in a changed file (call edges) plus every IMPORTER of a
   changed module (import edges) → the dependent file set with symbol
   anchors. Text vocabulary ("signature"/"parameter"/"drop the flag" vs
   "rename"/"every call site"/"all callers") classifies the shape; even
   without vocabulary hits, existing dependents still flag the change
   as coordinated (the GRAPH, not prose, decides atomicity). Protected
   paths (tests/*, VCS dirs) are EXCLUDED from suggested groups — a
   coordinated agent-edit group can never include a forbidden path
   (this was a real design bug caught by the e2e: the first version's
   "_detected" enforcement group swept tests/test_invoice.py +
   invlib/__init__.py in via import edges and made EVERY task on the
   fixture unachievable). Never raises; graph unavailable →
   detected=False, previous behavior.
2. **Planner prompt** gains a `## Coordinated-change fan-out` section
   (AFTER `## Retrieved context` — T3's difficulty predictor cut
   marker untouched) plus a `change_group` field in the plan schema
   and a step-rule about atomic groups. The planner DECLARING the
   group is the contract — detection is advisory (informs the
   declaration), never force-enforced: a detected group the plan
   declined to declare must not gate the task.

New trace event `coordination` {detected, kind, reason, changed_files,
dependent_files, group_files, excluded}.

### Task B — coordinated, ATOMIC multi-file patches

- **Plan schema**: steps may carry `change_group: <name>`.
  `_plan_change_groups` unions each group's files_hint;
  `state.json` gains the ADDITIVE `change_groups` key (after
  repo_path; Boundary 4 schema updated in INTERFACES.md; absent when
  none declared; resume-hydrated; malformed-tolerant).
- **Gate** (`coordination_gate`, default on): after the attempt's
  steps + final edit-validation, BEFORE final verify — a group with
  some-but-not-all members changed is a PARTIAL coordinated change:
  the attempt is poisoned (feedback names the missing members via
  `format_missing_group_feedback`), the verifier never sees it as
  complete. The verifier is NOT asked to re-litigate group
  completeness — a suite without call-site coverage would happily
  pass a half-updated rename (that's the entire point of the gate).
- **Rollback** (`editor.restore_group` + `group_orphans`): the
  declared group's files revert to pristine TOGETHER — including
  members that never changed and agent-created members (pristine
  state = absent → deleted). Non-group work in work/ SURVIVES (the
  config `coordination_rollback` selects "group" default | "all" |
  "none"). A FAILED verified attempt also rolls touched groups back
  together (atomic on failure, not only on partial). New
  `coordination_rollback` trace event {groups, restored, mode} +
  a state.json decision; `files_touched` is cleaned of rolled-back
  files (`TaskState.clear_files_touched`).

### Task C — a GENUINE 4-file coordinated scenario, end-to-end

**New fixture `tests/fixtures/bug06_coord`** (not a toy): an invoice
library where `Invoice.invoice_total(include_tax=False)` ignores its
flag and always returns the pre-tax subtotal. The correct fix — drop
the misleading parameter, rename to `amount_due()` returning
subtotal+tax — REQUIRES coordinated edits in model.py (definition),
serializers.py (2 call sites), reports.py (1), api.py (1): four files
that must change together; the suite's five tests all break otherwise.

**e2e proofs (tests/test_coordination_e2e.py, real Docker stack, all
green)**:
1. *Happy path*: scripted model applies the real 4-file fix via 4
   heredoc edits → detection event fired (structural fan-out), plan
   declared the 4-file group, gate passed, verified success in 1
   attempt, all four files in the diff, group + members in
   state.json.
2. *Partial change rejected + ATOMIC rollback*: attempt 1 renames
   model.py only (the classic half-updated state) → gate poisons the
   attempt pre-verifier with the three missing files named →
   `coordination_rollback` restores ALL FOUR (model.py too, though
   it was the one file that DID change — that's the atomicity
   claim proven) → attempt 2 completes the group → success at
   attempts=2, final diff = exactly the 4-file fix, only ONE
   final_verify ever ran (on the complete change).
3. *Complete-but-broken rolls back together*: all 4 files change but
   model.py computes tax twice → gate passes (group complete),
   final verify fails → group rolls back together → attempt 2's
   correct fix wins; broken arithmetic absent from the final diff.
4. *Regression guards*: plain single-file fix (bug02) with detection
   ON → coordination event present, detected=false, NO gate/rollback/
   groups; `coordination_detect=False` → no event at all (OFF arm).

**Unit suite (tests/test_coordination.py, 31 tests, no Docker)**:
vocabulary classification, real-graph fan-out on a synthetic repo
(callers+importers found via edges, changed files excluded, bounded,
no-dependents safe, graph-absent degrade), protected-path exclusion,
group completeness, plan parsing/group union, prompt blocks (incl. a
no-leftover-{coordination_rules}-slot guard — the planner template's
JSON braces forbid str.format, so the slot is .replace()d), atomic
restore_group semantics (untouched members revert too, agent-created
members deleted, non-group work survives, missing files never raise,
orphan-dir pruning), and the state.json additive-key invariants
(order, absence-when-unset, resume hydration, malformed tolerance,
files_touched cleanup).

### Docs/config/CI

- Config keys (harness/config.py): coordination_detect,
  coordination_gate, coordination_min_files, coordination_rollback.
- harness-ci.yml: both new suites in the selection + fixture image
  warm list (bug06_coord) + path filters.
- INTERFACES.md: Boundary 4 schema shows the additive change_groups
  key; Change Log entry filed (2026-09-10 Terminal 1).

### Test status at round close

**265/265 green** across the 13 landed harness suites (10:25 warm
Docker): the 166 pre-flight baseline + 31 coordination unit + 5
coordination e2e + the parallel session's batch_docs_lint (11),
agent_tests, decision_memory_planning suites that landed mid-round.
The two not-yet-collecting in-flight files remain the parallel
session's to finish (their harness halves — retrieval.rerank_files,
prompts.render_self_critique_prompt — were not in the tree at close
of my round; verified by import attempt, not assumption).

### Known limitations (this feature, honest)

- **Group declaration depends on the planner**: only plan-declared
  groups are enforced. A planner that ignores the fan-out section can
  land a partial coordinated change IF the suite happens to stay
  green (the gate needs the declaration to know the file set). The
  detection section + step rules make that unlikely, and the
  verifier still catches behavior-visible breakage — but
  "undeclared coordinated change" is out of the gate's reach by
  design (enforcing detection-suggested groups proved actively
  harmful: the tests/__init__ false-positive class).
- **Fan-out is name-resolution based** (memory.code_graph's documented
  over-approximation): `obj.foo()` edges to every `foo` method. For
  group SUGGESTIONS that's the right recall-over-precision trade; the
  declaration is where precision comes from.
- **files_hint is the group's file set**: a planner declaring a group
  but listing files loosely (e.g. forgetting one call-site file in
  files_hint) under-declares the enforced set. The prompt's fan-out
  section lists the graph-found dependents precisely to prevent this.

## Round 7 (2026-09-09) — production readiness: CI + architecture docs

(Round 6 landed in-flight in this tree before Round 7 started — its
adversarial hardening (editor `_ALWAYS_PROTECTED` VCS dirs +
traversal-normalizing `is_protected`, core final-edit re-validation
before the success-minting verify, tools deny-pattern reorder/bash
pipe fixes) + tests/test_adversarial.py were verified green FIRST:
150/150 harness tests incl. the 43 new adversarial ones, ~6 min warm
Docker. Round 7 built additively on top.)

### Task A — CI pipeline (the `harness` job in .github/workflows/ci.yml)

- **Matrix**: Linux/macOS/Windows × Python 3.10/3.12 (3.10 = the
  pyproject floor; 3.12 = current stable; litellm 1.74.9 pin holds on
  both). `fail-fast: false` so one cell's flake doesn't hide another's
  real failure. Runs on every push/PR, alongside T3's existing
  runtime/stress jobs (same file, unchanged).
- **What's in CI**: the harness module's own test selection (the six
  Round 1–5 suites + Round 6's test_adversarial.py — same list as the
  "Test status" section below), `-p no:randomly` for determinism.
- **Docker handling**: ubuntu runners ship Docker → Linux cells run
  the FULL suite incl. real-sandbox e2e (fixture dep images warmed
  first via `execution.sandbox.ensure_image`, same pattern as the
  stress job). macOS/Windows runners have no daemon → Docker-dependent
  tests self-skip CLEANLY via the existing `requires_docker` convention
  (skipif: daemon unreachable OR HARNESS_EXEC_SKIP_DOCKER=1, explicit
  reason string). **Verified by simulation, not assumption**: full
  suite re-run with HARNESS_EXEC_SKIP_DOCKER=1 → 142 passed, 8 skipped,
  each skip carrying the "docker daemon not reachable" reason — the
  exact behavior a Docker-less CI cell will show.
- A small Docker-availability diagnostic step prints CLI/daemon state
  per cell so skip counts in the logs are explainable at a glance.

### Task B — architecture documentation

- **`docs/architecture-harness.md`** (NEW): prose + ASCII flow + Mermaid
  diagram covering the core loop (setup → baseline verify → retrieval
  → plan → attempt loop → verifier gate → product output), the step
  session (bash-only, SUBMIT/RECALL escapes, deny-pattern guard), the
  retry/repair logic (last_feedback seam, both feedback loops, final
  edit re-validation, short-circuits), resume, the state.json
  contract + rules, the two-layer retrieval strategy, module map,
  and the logs layout. Written for someone who has NOT read the code.
- Linked from the root README (four-layers table + Status section).

### Test status at Round 7 close

**150/150 harness tests pass** (`python -m pytest
tests/test_config_trace_state.py tests/test_stubs_and_deps.py
tests/test_retrieval_tools.py tests/test_editor_prompts.py
tests/test_e2e_run_task.py tests/test_recall_unit.py
tests/test_adversarial.py`), ~6 min warm Docker — the 107 from Round
5 + 43 from Round 6's adversarial suite. Docker-less simulation of the
same selection: 142 pass / 8 clean skips. The CI job runs exactly this
selection.

## Round 6 (2026-09-09) — multi-repo validation (3 OSS repos) + adversarial robustness

### Pre-flight

Full Round-5 suite re-run first: **107/107 green** (~3.3 min, Docker warm),
Docker up, key present. Verified, not assumed.

### Task A — 3 more real OSS repos, varying size/structure (all SUCCESS)

Round 4 proved one repo (jaraco/path). Round 6 adds three MORE unfamiliar
repos — none ever used by any terminal before, deliberately varied in shape:

| repo | shape | introduced bug (genuine, failing-test-encoded) | result |
|---|---|---|---|
| r1chardj0n3s/parse | small utility lib, flat single-package | `Result.spans` for dict-style fields (`{quest[name]}`) keyed by the internal mangled group name (`quest_name_`) instead of the original field name — inconsistent with `r.named` (public-API leak of a Parser implementation detail) | **SUCCESS, 1 attempt, 10/10 checks, 47s** |
| bottlepy/bottle | mid-sized micro-framework, ENTIRE framework in one 4.4k-line file | `parse_range_header` lost the RFC 7233 end clamp (`min(int(end)+1, maxlen)`): a satisfiable `bytes=90-500` on a 100-byte file reads as unsatisfiable → 416, or serves a lying Content-Length | **SUCCESS, 1 attempt, 10/10 checks, 76s** |
| pallets/click | large CLI toolkit, MODERN src/ LAYOUT | `DateTime.convert` iterates `self.formats` in REVERSED order — for formats where two patterns parse the same input differently (`%m-%d-%Y` vs `%d-%m-%Y`), the caller-declared order must win (documented contract); ambiguous dates silently swap month/day | **SUCCESS, 2 attempts, 10/10 checks, 371s** |

Each bug was designed after genuinely reading the code (hours of live
probing per repo), verified as NOT covered by any existing upstream test,
and each repo's suite was pinned so it fails ONLY on the new regression
test (env limits documented: bottle's test_stpl/test_wsgi are Windows-CRLF
checkout artifacts; click's test_deprecations needs installed-package
metadata, test_stream_lifecycle is a 10-min subprocess-stress suite,
test_echo_via_pager needs `less` in the slim image — all deselected with
reasons, same class as R4's jaraco chown deselections).

**Every run went through the FULL stack**: real Scheduler → real
`python -m runtime.worker` subprocess → real `harness.core.run_task` →
real Docker sandbox/verify per command → verifier-gated success →
git branch/commit + rationale → real approval-gate file protocol (approve)
→ original repo untouched (probe-verified per repo) → behavioral
validation of the FIX in-sandbox (spans keyed correctly / ranges clamped /
formats tried in order) → full suite green on the fixed work copy.

**HONEST DOWNGRADE — the model layer was SCRIPTED, not cloud.** Two
real-model attempts failed on ENDPOINT DEGRADATION, not harness issues:
the tokenrouter glm-5.3 endpoint began burning its completion budget
entirely as hidden `reasoning_content` (content=None, finish_reason=length)
on the big planner prompt — twice at a 4000-token budget (1453s and 265s
calls), then again at 8000 (10271-token calls). Small prompts still worked
(verified: 20-70s, content present) — a large-prompt/reasoning-verbosity
failure mode, not an outage. R4 already proved real-cloud-model competence
end-to-end ($0.053, 6 calls); Task A's question was whether the HARNESS
generalizes across repo shapes — answered by scripted-model runs (T3's
`mock_script` mechanism through `Task.config`; the real loop, real
sandbox, real verify, real guards all executed). Cost of the failed
real-model attempts: ~$0.06 total; their traces preserved under
logs/oss-round6/oss-r6-parse* as the evidence.

**What the three repos actually tested (the generalization claim):**
- parse: flat-layout retrieval anchoring on a target test in a small
  package; patch-in-single-module flow.
- bottle: single-file framework — the bug and fix both live in ONE
  4.4k-line file; retrieval must anchor despite no package structure.
- click: src/ layout — the fix lands in `src/click/types.py` while tests
  run from repo root with `PYTHONPATH=src`; the pinned suite command
  carries the env override through every verify call.
All three: protected tests untouched, state.json/trace complete, approval
gate live, original repo never mutated. Report:
`logs/oss-round6/multi_repo_report.json` (10 checks per repo, all PASS).

### Task B — adversarial robustness: 43 regression tests; 2 real defects found+fixed

Attack matrix executed first as probes (logs/oss-round6/adv_probe*.py),
then encoded permanently in **tests/test_adversarial.py (43 tests, all
green)**:

1. **Protected-path guard (editor.py)**:
   - `is_protected` now '..'-normalizes paths BEFORE fnmatch — a
     traversal-shaped path (`subdir/../../tests/t.py`) can no longer evade
     a `tests/*` glob at the guard level (the real pipeline feeds
     rglob-normalized paths that can't contain '..', so production was
     safe — but the guard itself was evadable; defense-in-depth tested).
   - `.git`/`.hg`/`.svn` are ALWAYS protected, independent of
     `protected_paths` config: the snapshot drops .git, so any VCS path
     in a diff is an agent FORGERY of VCS state.
   - `check_edits` scans work/ for forged VCS paths on a separate pass —
     `changed_files` deliberately SKIPS .git content, so a diff-only
     check could never see the class (found by my own failing test).
2. **Deny-pattern guard (tools.py)**: covers reordered rm flags
   (`rm -fr /`), loose spacing, pipes into bash/zsh/dash from BOTH curl
   and wget (previously only `| sh`), fork bombs — while benign
   look-alikes stay allowed (relative `rm -rf build/`, plain curl,
   traversal READS — containment is the sandbox's job).
3. **Sandbox containment (verified e2e, Docker-gated)**: traversal
   writes (`echo > ../../../escaped.txt`) land on the container's
   read-only rootfs or inside the work mount — host-side pristine/ and
   the original repo byte-identical after hostile commands.
4. **Prompt injection e2e (the critical find)**: issue text with embedded
   "ignore previous instructions" ordering the agent to (a) defuse the
   test to `assert True`, (b) forge `.git/config`, (c) traversal-write
   outside the repo. Scripted model OBEYS. **Found a REAL success-path
   bypass in core.py**: the step-level guard correctly rejected the
   edits, but the attempt then fell through to final verify — which saw
   target+suite green (the defused test trivially passes AND the real
   fix was also applied) and minted `status="success"` with the
   protected-path violation sitting in work/. **Fix**: `run_task` now
   re-runs `check_edits` before final verify — an edit-policy violation
   poisons the whole attempt (retry with explicit feedback), the
   verifier never re-litigates policy. Regression-tested end-to-end
   (the same injection now fails every attempt; host integrity asserts).
   Also regression-tested: the whole-tests-dir RENAME evasion (mv tests
   tests.bak + trivial new tests) — the deletions show up as protected
   paths and the run is blocked.

**Real-model driver bug found during Task A runs (worth keeping):** the
Round-6 driver initially used `run_id=f"oss-r6-{name}"` COLLIDING with
the task_id — the scheduler's run dir (`logs_root/run_id/task_id/`)
nested INSIDE the harness log dir (`logs_root/task_id/`), so
`_fresh_paths`' archive rename hit Windows' open-handle restriction
(worker's inherited worker.log handle) → `PermissionError(13)` before any
trace event, 3 crash-retries, error. Root-caused from worker logs; R4's
driver used a distinct run_id (`oss-round4-run`) and never saw it.
Lesson for all: **scheduler run_id must never equal a task_id sharing
its logs_root** (documented here rather than changing T3's code — the
scheduler can't know task_ids in advance; convention is enough).

**Runtime-owned edits this round (T3's files, flagged per cross-terminal
practice — see INTERFACES.md Change Log 2026-09-09 Terminal 1):**
- `runtime/worker.py` + `runtime/model_router.py`: opt-in
  `max_completion_tokens` ctx key → litellm `max_tokens` (absent =
  previous behavior). Motivated by the endpoint's reasoning-burn failure
  mode above. T3's full suites re-run green after the edit (48 passed,
  3 cloud self-skips) — including the AuthenticationError no-retry
  test, whose intent I probed changing and deliberately REVERTED (the
  module's own test documents the contract; the observed gateway auth
  flake stays a health note in the run evidence).

## Round 5 (2026-09-09) — CLOSEOUT: item 13 finished, state flag fixed, suite green

### Task A — RECALL: on-demand reinjection of compacted detail (closes the item 13 gap)

The Round 4 self-audit honestly flagged that "reversible compaction" was
only met by design — trace.jsonl kept everything but nothing could pull
older detail BACK into a live session. That gap is now closed as a real
mechanism:

- **The protocol**: a step session outputs `RECALL <terms>` in place of a
  bash command. `harness/core.py` `run_step` intercepts it BEFORE command
  extraction (both the raw reply AND its fence-stripped form — a fenced
  ` ```bash\nRECALL x\n``` ` must never execute as shell; that fall-through
  was a real defect caught by the sub-agent's characterization test),
  greps THIS task's trace.jsonl, and re-injects the matching entries into
  the live session's context as the next user message.
- **The retrieval primitive**: `TraceLogger.find_events(query, kinds=None,
  limit=5, max_chars=4000)` (harness/trace.py) — case-insensitive substring
  over (event kind + JSON data dump), most-recent-N in chronological
  order, per-entry char cap with truncation marker, malformed lines
  skipped, file errors → [], never raises mid-step.
- **Budgets (config-driven, no constants)**: `max_recalls_per_step` (3) —
  a step must still do its work in bash turns; exhaustion nudges back to
  bash/SUBMIT, never deadlocks (tested). `recall_results_cap` (5),
  `recall_max_chars` (4000).
- **Observability**: new `recall` trace event {step_id, turn, query,
  matched}; step system prompt documents the escape to the model
  ("Recovering compacted-away context (RECALL)").
- **Proof it's real**: e2e test where step 1's session observes a marker
  via `echo` (compacted away by the per-step context reset), step 2's
  FRESH session RECALLs it, and the fake model only completes the fix
  after the marker demonstrably arrives in ITS OWN message list —
  content-receipt proof, not code-reading. Cross-session reinjection
  through the real Docker stack, real trace file.

Sub-agent note (per round instructions): delegated the RECALL unit-test
suite (tests/test_recall_unit.py, 26 tests) to a general sub-agent — it
performed well, delivered green tests, and caught a genuine defect in my
core.py fall-through (fenced RECALL executed as bash) that I then fixed;
its two docstring nits (spec-vs-code wording) were resolved by editing the
docstrings. The sub-agent approach worked; e2e tests stayed in-terminal
(they needed accumulated loop-semantics context).

### Task A2 — T3's state.json flag (cli-real-smoke) fixed

Terminal 3's Change Log flag: a successful task's final state.json showed
`completed_steps: []` + `remaining_plan: [step]`. Root cause (diagnosed
from that run's actual trace, not guessed): the step's commands had
applied the fix, but `max_step_turns` (6, pinned in the smoke config)
exhausted before the model's SUBMIT — `step_end ok=false
"exhausted ... turns"` — then final verify passed and the task succeeded,
with `complete_step` never called. Not resume-specific (the kill happened
pre-plan).

**Fix**: on a VERIFIED success, `run_task` records every plan step that
RAN in the winning attempt as completed (new
`TaskState.complete_all_ran_steps(steps)` in harness/context.py — the
verified diff subsumes each ran step's work; steps skipped by early-exit
never needed to run). state.json now agrees with the verified result;
`harness status` renders true progress on that path. Schema unchanged.
Regression test: exhausted-turns-with-real-fix → success AND
completed_steps == full plan, remaining empty.

### Task B — final suite + honest closeout

**107/107 green** (78 prior + 26 RECALL unit + 3 new e2e), ~4 min warm
Docker, full command in the Test status section below.

## Round 4 (2026-09-09) — self-audit + first full-stack run on an unfamiliar OSS repo

### Pre-flight: Round 3 verified landed (not assumed)

Full suite re-run first: **75/75 green** (~3 min, Docker warm), context.py
repair + regression tests present, e2e docstring updated, git/rationale
wiring live, approval tests present. All four Round-3 tasks confirmed.

### Task A — self-audit vs project-spec.md items 1–17 (all CORE)

Went through each item against the actual code, one by one:

| # | Item | Verdict |
|---|---|---|
| 1 | Repo understanding / retrieval | **Solid** (two layers: structural via memory.code_graph + target-test anchor + subword matching, grep fallback; best-effort degrade tested). |
| 2 | Tool interface / action space | **Solid** (bash-only per mini-swe-agent decision; fenced-block/heredoc-safe extraction; deny-pattern guard; SUBMIT semantics). |
| 3 | Patch generation + validation | **Solid** (check_edits: protected paths + syntax before verify; unified diff vs pristine; snapshot/restore). **One real defect found+fixed this round** (see Task B). |
| 4 | Sandboxed execution | **Solid** — real Docker sandbox auto-resolved via deps.py since Round 2 (fresh container per call, networkless, resource-limited); fail-loud SandboxUnavailableError, no silent fallback (deliberate). |
| 5 | Verification beyond "tests pass" | **Solid** (baseline verify on pristine pre-edit — mislabeled task short-circuits; flake = 3-valued outcomes incl. timeout; full-suite regression after target). |
| 6 | Within-task state/memory | **Solid** (state.json + plan.json; tried-work tracked; `files_touched` recorded only after validation; feedback flows step→step and attempt→attempt with verifier raw output). |
| 7 | Stopping conditions | **Solid** (max_retries, budget_cap_usd, max_wallclock_s — all config-driven, checked every iteration + mid-step deadline). |
| 8 | Logging / traceability | **Solid** (trace.jsonl: every prompt, response, tool call/result, verify, decision, RECALL; survives relaunches via resume append). |
| 9 | Model/provider abstraction | **Solid** (Boundary 2 via deps; router decides when model=None; per-call cost/tokens via get_last_usage convention; proven live on a real cloud tier this round). |
| 10 | Config / reproducibility | **Solid** (full merged config logged in task_start event minus api_key; plan.json captures attempt/cost). |
| 11 | Persistent structured state file | **Solid** (state.json, exact Boundary 4 schema, atomic tmp+replace write; regression tests hold the invariant; Round 5: agrees with verified results even on the exhausted-turns path). |
| 12 | Context resets at checkpoints | **Solid** (per-step FRESH sessions; per-step system prompt rebuild; completed-steps handed over structurally, not as chat history). |
| 13 | Reversible compaction | **SOLID as of Round 5** (was "met by design, honest caveat" in R4): trace.jsonl keeps everything, state.json is the compacted view, and the **RECALL protocol now pulls older detail back into a live session on demand** — budgeted, trace-logged, tested cross-session with content-receipt proof. |
| 14 | Task decomposition 2–4 sub-steps | **Solid** (planner enforces 2–4 with per-step checkpoint; no "run the suite" steps; early verifier exit when done mid-plan). |
| 15 | Constraint re-injection | **Solid** (re-injection block appended to END of every tool result — issue one-liner, current step, remaining, protected paths, don't-touch-tests). |
| 16 | Curated per-step context | **Solid** (step_files = step hint + retrieval, capped files/lines; not a static bundle). |
| 17 | Verifier-gated completion | **Solid** (success ONLY on verify() target+regression+not-flaky; SUBMIT ends a step, never a task; pre-passing pristine short-circuits to success with zero calls). |

**Nothing missing.** The two Round-4 weak spots: (a) item 13's reinjection
gap — **CLOSED in Round 5 (RECALL)**; (b) no failure CLASSIFICATION —
remains a documented, accepted limitation (see Known limitations below).

### Task B — full-stack DoD run on jaraco/path (unfamiliar real OSS repo)

**Repo**: jaraco/path @ 67319bb (fresh clone; NOT one of the 5 fixtures,
never used by this project before). After a genuinely-thorough hunt
(masks/matchers/classes/in_place/write_text/chunks/hashes/times/
merge_tree/only_newer/relpath/Multi/TempDir/walk all checked live —
upstream is solid), I **introduced one genuine bug**: dropped
`in_place()`'s permission preservation (`os.open(self, os_mode, 0o666)`,
no chmod re-apply — the function's own contract says "same permissions").
A 0o600 config file silently becomes 0644 after an edit — a real
security-adjacent defect class, encoded in a failing regression test
(`tests/test_in_place_perms.py`, both success and error-restore paths).
Bug verified reproducing in-sandbox (0o600→0o644); rest of suite green
with a pinned test command (`-o addopts=` strips --doctest-modules +
pytest-ruff pseudo-tests; test_chown/test_group deselected — they need
/etc/passwd entries absent from the slim image, an env limit, not the bug).

**Run 1 — FAILED, and that's the finding.** Full real stack (real
Scheduler → worker subprocess → real harness → real cloud model via
tokenrouter → real Docker sandbox; T3's own ablation was concurrently
hammering the same endpoint, so planner calls ran slow) — the agent
FIXED the bug, final verify PASSED (target + full suite), and then the
worker CRASHED with `UnicodeDecodeError: b'SQLite format 3...'`. Root
cause (mine, harness/editor.py): (1) `unified_diff` read changed files
with `errors="strict"` OUTSIDE its try/except — the repo's pytest run
materializes a binary `.coverage` (SQLite) in work/, counted as a
"changed file", and the strict decode raised instead of returning
None; (2) verifier-created artifacts (`.coverage`, cache dirs) counted
as agent edits at all. A **verified fix was reported as "error"** after
3 crash-retries exhausted (each resume → re-verify → recreate .coverage
→ crash again — a textbook crash loop).
**Fix (harness/editor.py)**: strict decode moved inside the
UnicodeDecodeError guard (binary → reported, never raised);
`changed_files` skips run artifacts (`.coverage*`, `.hypothesis`,
`.cache`, + prior skip set). **3 regression tests added**
(binary-not-crash, binary-reported, artifacts-ignored). 78/78 suite
green post-fix.

**Run 2 — SUCCEEDED end-to-end** (`oss-path-perms-r2`, 10/10 checks,
driver + corrected validator under logs/oss-round4/): Scheduler →
worker → real harness → **real cloud model (z-ai/glm-5.3-free via
tokenrouter, 6 calls, $0.053)** → every command through the real Docker
sandbox → verifier-gated **success in 1 attempt, 373s**. The agent wrote
its own (equivalent, cleaner) fix — capture `original_mode` before
rename, `os.chmod` after create — NOT the literal upstream shape (my
validator initially demanded the literal shape and failed; lesson
recorded: validate BEHAVIOR, not implementation text). Confirmed live:
git branch `harness/fix-bug-report-...` with pristine-first commit whose
`git show` IS the fix; rationale.md grounded in the trace; **approval
gate ran the real file protocol** (request.json → external approver →
decision.json → approved → git output composed); original repo
untouched (bug still present there); regression tests re-run green on
the fixed work copy IN-SANDBOX; **Terminal 4's memory ingested the
run's decisions via the real recursive poll (source: state-file) and
serves them through the MCP `query_decisions` surface** — verified by
query, not assumption. Full report: logs/oss-round4/oss_run_report.json.

**Cross-module notes from the run:**
- T3 scheduler/worker/resume behaved exactly per contract under a real
  crash-retry loop (crash→retry→resume→crash→exhausted→error status);
  the hang-check pin (`hang_heartbeat_stale_s` 1200 ≥ slow model calls)
  worked as documented.
- T2's sandbox/verify were transparent throughout; the ONLY issue was
  my editor.py binary handling (T2's DoD had hit semver's symlink quirk
  but not the binary-artifact one — different repo, different quirk).

**Test status: 78/78 pass** (75 prior + 3 editor regression), ~4.5 min
with warm Docker.

## Round 3 (2026-09-08) — patch review, git-native output, approval e2e

### Task A — Terminal 4's repair of harness/context.py: REVIEWED, CORRECT

An interrupted edit of mine (the Round-2 plan.json work) had left a
duplicated dangling `def _write` stub in context.py; Terminal 4 found it
live (every `import harness.*` raised IndentationError, caught during
their CLI e2e) and deleted exactly the dangling stub — nothing else.
**Verdict: the repair matches my intended final design.** The intended
shape is and was: ONE `_write` (the atomic tmp+replace Boundary-4 state
writer) and `save_plan_steps` writing ONLY plan.json via `PLAN_FILE`
(separate file, separate writer). The duplicate stub was an artifact of
the interrupted edit, not a method I meant to keep.
**Regression tests added** (`tests/test_config_trace_state.py`):
- `test_harness_modules_import_and_parse` — AST-parses every harness
  module + import-checks each (catches the un-parseable-file class at
  collection time, before any downstream module breaks).
- `test_context_has_single_wellformed_write_method` — structural
  invariant: exactly one `_write` containing the tmp+replace pair;
  `save_plan_steps` must not call `self._write(`.
**Proven live, not just written**: a throwaway script re-introduced the
exact corruption (dangling stub before `_write`), reproduced the
identical `IndentationError ... line 196`, tests failed at collection,
file restored, 12/12 green again. (Would have caught the bug originally.)

### Task B — stale docstring fixed

`tests/test_e2e_run_task.py` module docstring now says "REAL Docker
sandbox and verifier (execution.sandbox / execution.verify via deps.py
auto-resolution; the local subprocess stub is fallback-only)".

### Task C — git-native output + rationale wired into run_task

On a VERIFIED fix, `run_task` now produces real git artifacts in the
harness's PRIVATE work copy (original repo never touched, by
construction — git ops only ever run in `logs/{task_id}/work/`):
- `execution.git_output.produce_git_output(work_dir, issue_text,
  changed_files, diff, verification_summary, rationale, branch_name,
  pristine_dir)` → git init + pristine first commit + fix commit on a
  `harness/fix-<slug>` branch. Result dict {branch, commit_sha,
  commit_message, pr_description} → `logs/{task_id}/git.json` + a
  `git_output` trace event. Proven: the fix commit's `git show` diff IS
  the fix; author is harness-bot; two commits on the branch.
- `execution.rationale.build_rationale(log_dir, issue_text)` → one
  grounded paragraph from trace.jsonl + state.json → written to
  `logs/{task_id}/rationale.md` + a `rationale` trace event. Written on
  success paths (after `task_end`, which the verdict keys off) AND on
  failed/timeout outcomes (`_record_rationale_only` — a rationale is
  valuable on losses too; an unverified diff NEVER gets git output).
- Both best-effort BY CONTRACT: any failure degrades to a
  `rationale_failed`/`git_output_failed` trace event, never changes the
  verifier-gated task outcome (new config keys `git_output`/`
  rationale_log`, both default True; `branch_name` optional pin).
- e2e proof: 3 Docker-gated tests (produces artifacts + disabled-path
  produces none + failed-path gets rationale but no git output).

### Task D — approval-mode wiring CONFIRMED LIVE

The gate lives in Terminal 3's worker, wrapping my Boundary-3 run_task
(the runtime's documented design). Tested end-to-end, not by reading:
real `Scheduler` → real `python -m runtime.worker` subprocess → REAL
harness loop (scripted fix through real Docker verify) → real
request.json/decision.json file protocol with an external approver
thread. Both paths proven: APPROVE (task finishes success, diff applied,
review.log = requested→approved, worker events show approval_wait →
approval_granted → worker_finish, AND Task C's git output composes —
branch exists in work/) and REJECT (result downgraded to failed, diff
stripped, never applied).
**Real cross-module finding for Terminal 3**: a worker blocked in the
approval gate stops touching state.json; the scheduler's hang check
(default `hang_heartbeat_stale_s: 30`) KILLS it mid-gate. Mitigation
(documented pattern, now in my tests): pin `hang_heartbeat_stale_s` ≥
`approval_timeout_s`. T3: consider making the worker beat the
checkpoint heartbeat while parked in the gate — the heartbeat daemon
thread already exists in your worker; state.json staleness is the
hang signal, heartbeat is liveness, and a gate-blocked worker is alive.

**Enabling hook for cross-process determinism** (new,
`harness/_stubs/scripted_model.py` + `deps.get_call_model`): env var
`HARNESS_SCRIPTED_MODEL=<json spec>` resolves a file-driven
`ScriptedFileModel` (same dispatch contract as tests/fake_model) inside
worker SUBPROCESSES, where `set_call_model` injection cannot reach.
Cached per spec-path per process (ModelClient re-resolves per call; the
script queue must survive). Test-only hook by design; in-process tests
keep using `set_call_model`.

**Test status: 75/75 pass** (68 prior + 2 context regression + 3
git-output/rationale e2e + 2 approval e2e), ~3 min with warm Docker
images.

## What's built (complete module state, Rounds 1–5)

The full agent loop: given a `Task` (issue + repo), retrieve context, plan
sub-steps, edit a working copy via bash, verify, and retry with feedback —
implemented per `INTERFACES.md` Boundaries 3 & 4 and the context-management
ideas in `project-spec.md` (items 12/13/14/15/16/17, with 13 fully closed
in Round 5 via RECALL).

| File | Role |
|---|---|
| `core.py` | **`run_task(task, log_root=None) -> TaskResult`** (Boundary 3). Attempt loop, per-step sessions, stopping conditions, verifier-gated completion, **resume contract (Round 2, see below)**, **product-grade output on verified success (Round 3: git branch/commit/PR + rationale)**. |
| `context.py` | Structured state file `logs/{task_id}/state.json` in the exact Boundary 4 schema (atomic rewrite; `reset_completed()` on retry rollback; **resume hydration + `plan.json` bookkeeping**). **Round 3: Terminal 4's mid-edit repair reviewed + regression-tested — see above.** |
| `trace.py` | Append-only `trace.jsonl`: every prompt, model response, tool call, tool result, verify, decision, RECALL. **Survives relaunches (appended to on resume).** **Round 5: `find_events()` — the reversible-compaction retrieval primitive (RECALL queries it).** |
| `config.py` | All tunables from `task.config` merged over `DEFAULTS` (retries, budget, wall-clock, caps, protected paths…). |
| `retrieval.py` | **Two layers (Round 2)**: structural (imports/call-graph via Terminal 4's `memory.code_graph`, anchored on the target test + subword symbol matching) merged with the Phase-1 grep layer. Returns a `strategy` note. |
| `tools.py` | **Bash-only action space** (mini-swe-agent style): one command per turn, output-capped, `SUBMIT` ends a step, deny-pattern guard. **Round 5: `parse_recall` — the RECALL escape (control signal, never executed; checked raw AND fence-stripped).** |
| `editor.py` | Snapshot/restore of working copies, difflib unified diff, pre-verify validation (protected paths, Python syntax). |
| `prompts.py` | Planner prompt (2–4 sub-steps, each with checkpoint — no one-shotting; now states the retrieval strategy), step-session prompts, **constraint re-injection block appended to every tool result**. |
| `model_client.py` | Wraps Boundary 2 `call_model`: trace logging, usage/cost accounting via `get_last_usage()` convention, `TaskResult.model_calls` records. |
| `deps.py` | **The swap point**: tries real `execution.sandbox` / `runtime.model_router` / `memory.code_graph` first, falls back to `harness/_stubs/`. Tests inject fakes via `set_call_model` / `set_execute_sandboxed`; **cross-process tests use the `HARNESS_SCRIPTED_MODEL` env hook (Round 3)**. |
| `_stubs/` | Local stubs with exact contract signatures: subprocess sandbox, pytest-based `verify()`, single-provider litellm router (lazy import), **`scripted_model.py` (env-driven fake for subprocess tests)**. **All three real modules have landed; stubs are fallback-only.** |

## Round 2 changes

### Task A — resume/checkpoint contract (fixes the cross-module bug)

**Bug**: `_fresh_paths` archived `logs/{task_id}/` wholesale on every
relaunch, so Terminal 3's runtime-side checkpoint/resume could never
actually resume — state.json (the progress authority) was swept away
before the relaunched run could read it.

**Fix** (core.py + context.py):
- `run_task` now implements the resume contract: when
  `task.config["resume"]` is truthy AND the prior `state.json` has
  completed steps AND a readable `plan.json` exists → **resume**, else
  the old archive-then-fresh behavior (unchanged for non-resume callers —
  regression-tested).
- Resume keeps `logs/{task_id}/` intact: hydrates `TaskState` from the
  real state.json (decisions/files_touched/completed_steps preserved),
  **reuses the persisted plan** (new `plan.json`, harness-internal —
  re-planning could orphan recorded step descriptions), skips completed
  steps (`step_skipped_resume` trace event), keeps the surviving
  `pristine/`+`work/` dirs (partial edits are built upon, not thrown
  away), continues the **in-flight attempt** (a crash is an
  interruption, not a verification failure — no `max_retries` slot
  consumed), and seeds the budget cap with the pre-crash spend.
- Baseline verify is skipped on resume (a passing baseline would have
  ended the pre-crash run before any step completed; re-running it
  cannot change the outcome).
- `trace.jsonl` is appended to across relaunches — pre-kill events
  survive as first-class history.
- Degrades safely: unreadable state.json/plan.json or missing
  pristine/work → fresh start (trace event `resume_aborted`), never a
  crash or nonsense-resume.

**Proof**: `tests/test_e2e_run_task.py::test_resume_after_hard_kill_mid_run`
mirrors Terminal 3's scheduler scenario through the REAL loop: a child
process (tests/resume_driver.py) is hard-killed via `os._exit(70)` after
step 1 of 2 completes, the relaunch resumes from the real state.json,
skips step 1, finishes step 2 from the surviving partial edit, and
succeeds with attempts=1. Plus: non-resume relaunch still archives
(regression guard), corrupted state → fresh, missing copies → fresh.

### Task B — smarter context retrieval (Phase 2 scope)

`retrieval.py` grew a structural layer on top of the Phase-1 grep layer:
- Consumes **Terminal 4's `memory.code_graph`** (tree-sitter; via
  `deps.get_code_graph_factory()`) — one structural index for the whole
  project, no harness-private duplicate. Explicitly invited by
  memory/AGENTS.md.
- **Target-test anchor** (the key mechanism): the target test's file →
  its import edges → the module under test; its call edges → the symbols
  it exercises. Finds the right files even when the issue text names
  NOTHING (proved by test: issue "something is off in how numbers are
  summarized" → `numlib/mathutil.py` via anchor).
- **Subword symbol matching**: `compute_monthly_total` decomposes to
  {compute, monthly, total}, so an issue saying "monthly total is
  doubled" matches without the identifier ever appearing in the issue.
- **Call-graph neighborhood**: matched symbols expand one hop to direct
  callers/callees (the failing test is often the caller; the true defect
  often a callee).
- Structural scores outrank grep hits (×2 merge weight); the index lives
  at `logs/_code-graph/` (SHARED across tasks, OUTSIDE the original repo
  — the never-mutate guarantee holds; regression-tested) and is reused
  across tasks on the same repo (mtimes don't change on a read-only
  repo).
- Fully best-effort: graph module missing/broken → grep-only with
  `strategy: "grep"` (tested via monkeypatched broken factory). No
    embedding layer — true synonym gaps ("average" vs `mean()`) remain
  future work (would need an embedding index; documented below).
- Planner prompt now tells the model which strategy produced its
  context; `trace.jsonl` records the retrieval decision
  (`retrieval` event: strategy/terms/files).

### Task C — Terminal 2's real sandbox/verify: LANDED and verified

`deps.py` auto-resolution picked up `execution.sandbox` +
`execution.verify` the moment they landed — **no code changes needed**.
Verified explicitly:
- `verify(repo_path, target_test, rerun_for_flake_check=1,
  test_command=None, verify_timeout_s=300, *, allow_network=False)` —
  the `test_command`/`verify_timeout_s` kwargs I flagged in the Change
  Log are honored exactly (defaults match; used for both target and
  suite runs, node ids appended).
- `execute_sandboxed` = exact Boundary-1 signature + safe keyword-only
  extras. My e2e suite (real bash through real Docker) passes: 20/20.
- **Deliberate non-change**: Terminal 2 suggests core.py could catch
  `SandboxUnavailableError` and fall back to the subprocess stub. I
  did NOT add silent fallback — "sandboxed must mean sandboxed"; a
  down Docker yields task status "error" (loud, diagnosable, and the
  runtime's crash_retries governs relaunching). Opt-in degraded mode
  can be a future config key if ever wanted.
- Sandbox timeout convention (exit 124 + timed_out=True) matches the
  stub's, so my step-session and feedback logic is unchanged.

## Design decisions worth knowing

- **Agent never touches the original repo.** Every run snapshots the repo
  into `logs/{task_id}/pristine/` + `work/`; diffs are computed between
  those; `restore_dir()` rolls back between attempts. A stale
  `logs/{task_id}/` from a prior non-resume run is archived as
  `{task_id}.old-*`, never deleted; a RESUME relaunch keeps it (Task A).
- **Verifier-gated completion is absolute.** `status="success"` is only
  set when `verify()` confirms target test pass + full-suite regression
  pass + not flaky. `SUBMIT` only ends a step session. If the target
  already passes on the pristine copy, the task short-circuits to success
  with zero model calls (proved by an `ExplodingModel` test).
- **Model defaults are `None`** in `config.DEFAULTS` (model/provider/
  api_key). An unset value means "let the router decide" — Terminal 3's
  adaptive routing depends on this; pin values in `task.config` to force
  a model.
- **Multi-line commands are first-class.** `_extract_command` keeps
  fenced blocks and bare heredocs whole (a beheaded `python - <<EOF`
  heredoc was a real test-caught bug).
- **Constraint re-injection rides every tool result** (end of message =
  max recency): issue one-liner, current step, remaining steps, protected
  paths, "don't modify tests".
- **Failure feedback flows both ways**: step-to-step (intra-attempt) and
  attempt-to-attempt (after rollback). Feedback includes the verifier's
  raw output tail, not just "it failed".
- **Resume granularity is step-level** (state.json's granularity): a
  crash mid-step re-runs that step from its partial edits in work/; a
  crash after a step-completion skips it. `plan.json` (steps +
  in-flight attempt + spent cost) is the harness-internal bookkeeping
  that makes "continue the interrupted attempt" well-defined.

## What's stubbed / mocked, and why

- **All three real boundary modules have landed** (execution.sandbox,
  execution.verify, runtime.model_router, memory.code_graph); `deps.py`
  resolves them automatically. `harness/_stubs/` remains only as the
  import-error fallback (e.g. Docker-less dev machines) and as the
  explicit "want the old behavior" import path per Terminal 2's AGENTS.
- Tests use `tests/fake_model.py` (`ScriptedModel` / `ExplodingModel`)
  injected through `deps.set_call_model` — no network, deterministic,
  real bash + real Docker sandbox + real pytest runs in e2e tests.
  `tests/resume_driver.py` runs the real loop in a child process so
  hard-kill tests don't take pytest down with them.

## Definition of Done — where it stands

Five genuinely different bugs (boundary condition, off-by-one, missing
guard, undefined name, mutable default) in five fixture repos under
`tests/fixtures/bug0{1..5}_*/`. The e2e suite fixes all five with real
bash commands through the full stack (now against the REAL Docker
sandbox), each producing a valid `state.json` (exact Boundary 4 key
order) and complete `trace.jsonl`. Loop behaviors covered by tests:
verifier-gating (model claims SUBMIT without fixing → task FAILS),
retry-recovery (syntax-error attempt → clean fix on attempt 2),
max-retries, budget cap, wall-clock timeout, protected-path rejection
(agent tries editing tests → blocked), original-repo-never-mutated, and
(Round 2) hard-kill → relaunch → resume; non-resume relaunch archives;
corrupt/missing resume state degrades to fresh.

**(Round 4) Definition of Done now ALSO proven on an unfamiliar real
OSS repo** — jaraco/path, a deliberate security-adjacent bug
(in_place() permission loss), full stack incl. real cloud model,
approval gate, git output, rationale, and memory ingestion: SUCCESS in
1 attempt ($0.053, 6 model calls, 373s). The first attempt's honest
failure is part of the record: it exposed and fixed a real harness
defect (editor.py binary/artifact handling — see Round 4 above) that
the 5 controlled fixtures could never catch. Evidence:
`logs/oss-round4/` (driver, validator, oss_run_report.json, both runs'
full logs).

**(Round 6) DoD now proven on FOUR unfamiliar OSS repos total** — the
R4 repo plus parse (flat utility), bottle (single-file framework), and
click (src-layout) — three MORE shape-varied repos each with a genuine
introduced bug, all fixed verifier-gated through the full stack (see
Round 6 Task A for the honest scripted-model account and the endpoint
degradation evidence). Evidence: `logs/oss-round6/multi_repo_report.json`.

**Test status: 150 harness tests pass** (`python -m pytest
tests/test_config_trace_state.py tests/test_stubs_and_deps.py
tests/test_retrieval_tools.py tests/test_editor_prompts.py
tests/test_e2e_run_task.py tests/test_recall_unit.py
tests/test_adversarial.py`), including the 4
resume tests, 6 structural-retrieval tests, (Round 3) 2 context-regression
+ 3 git-output/rationale e2e + 2 approval-mode e2e tests, (Round 4) 3
editor binary/artifact regression tests, (Round 5) 26 RECALL unit
tests + 3 new e2e (cross-session RECALL reinjection, RECALL
budget-exhaustion, exhausted-turns success-state), and (Round 6) 43
adversarial tests (protected-path traversal/VCS-forgery, deny-pattern
matrix, sandbox containment, prompt-injection e2e, rename evasion).
All Docker-gated suites per Terminal 2's skip convention.

## Known limitations (honest, accepted)

- **Failure classification is NOT implemented — deliberately.** Retry
  strategy is naive-retry-with-rich-feedback (verifier output tails flow
  step→step and attempt→attempt at the documented `last_feedback` seam).
  The chosen novel mechanism for this project was ADAPTIVE MODEL ROUTING
  (Terminal 3's difficulty prediction — measured: ~2.6-3.5x cheaper at
  equal success), not repair classification; the `last_feedback` seam is
  where spec item 24/25 could plug in if ever revived. Accepted as the
  documented scope boundary, not a silent drop.
- **RECALL matching is substring, not semantic**: a RECALL finds what the
  terms literally mention in the trace (case-insensitive). True synonym
  recall (query "average" for `mean()`) would need embeddings — same
  vocabulary gap as retrieval, same future-work seam.
- **Docker-less dev machines** fall back to the local subprocess sandbox
  stub via deps.py import-error resolution (fail-loud in production: no
  silent unsandboxed fallback when the real sandbox raises
  SandboxUnavailableError).

## What's next (Phase 3 hooks, not started — none are CORE)

- Embedding-based retrieval for true synonym gaps (issue says "average",
  symbol is `mean()`) — subword matching covers identifier decomposition
  but not vocabulary; same seam would upgrade RECALL from substring to
  semantic matching. Would need a local embedding index (ChromaDB
  experience per spec Phase 2). (Would ALSO fix decision-memory query
  recall: the memplan ablation's initial bug04 seed row missed its
  query purely on keyword overlap — see Improvement Round 2 Task B.)
- ~~`query_structure`/`query_decisions` woven into planning~~ — **DONE in
  Improvement Round 2 (2026-09-10)**: the planner queries decision
  memory repo-scoped before planning and injects it as a prompt section;
  measured in a real-model ablation (mistake recurrence 3→0, -23%
  model calls, at equal success). The MCP-wire form is unnecessary
  in-tree (same process tree, programmatic store access); step-session
  weaving remains possible future work if steps ever need mid-task
  memory.
- Failure classification feeding repair strategy (spec item 24/25, the
  documented `last_feedback` seam) — see Known limitations; deliberately
  not the chosen mechanism.

## Known issues / flags for other terminals

- **Terminal 2 (execution):** your verify() kwargs landed exactly as
  requested — closed out, thanks. Not adopting the suggested
  SandboxUnavailableError→stub fallback (fail-loud is the right
  default; see Task C note above). Round 4 note: repos whose test
  setups materialize run artifacts in the repo dir (pytest-cov's
  `.coverage` SQLite, cache dirs) used to poison my diff/files_touched
  — fixed harness-side (editor.py skips them + binary-safe decode);
  no execution-side action needed, but worth knowing the artifact
  class exists beyond semver's symlink quirk.
- **Terminal 3 (runtime):** the resume contract is LIVE — your worker's
  `cfg["resume"]=True` + state.json gate now actually resumes the real
  harness (plan reuse, step skip, attempt continuation, budget seed).
  No runtime-side changes needed. Your 3 scheduler-test failures noted
  in Round 1 were yours (fake-harness-pinned) and untouched by this
  round. Round 4 validation: your scheduler/worker handled a real
  crash-retry-exhaustion loop exactly per contract under a REAL harness
  bug, and the hang-check pinning pattern (`hang_heartbeat_stale_s` ≥
  slow model calls) held with a real cloud model. **Round 5: your
  cli-real-smoke state.json flag is CLOSED** (root cause + fix in Round 5
  Task A2 above; Change Log entry filed) — `harness status` now renders
  true progress on the exhausted-turns-success path.
- **Terminal 4 (memory):** `retrieval.py` now consumes
  `memory.code_graph.CodeGraph` programmatically (load_or_build + raw
  Graph nodes/edges; NOT the string `query()` surface). Contract
  addition logged in INTERFACES.md. `state.json` schema unchanged;
  `decisions` now also records the resume decision. Keep the
  Graph/NodeInfo dataclasses stable-ish or flag in the Change Log.
  Round 4: your recursive poll + MCP query_decisions surface ingested
  and served the OSS run's decisions from the REAL logs tree with zero
  changes — confirmed by query, not assumption.
  **Improvement Round 2 (2026-09-10): the harness is now your store's
  second PROGRAMMATIC consumer** - the planner queries
  open_default_store().search(..., repo_path=task.repo_path) before
  every plan. Keep search's repo_path kwarg + open_default_store stable
  or flag in the Change Log. state.json now writes the ADDITIVE
  repo_path key (your ingest_state_file already read it); the
  decision_memory trace event is safe to surface. The memplan ablation
  pins HARNESS_DECISIONS_DB per-run - your default location convention
  is unchanged.
- **All:** `logs/_code-graph/` is the shared structural index root
  (repo-keyed subdirs); `logs/{task_id}/plan.json` is
  harness-internal (not Boundary 4) — don't parse it from outside the
  harness. Round 5: new `recall` trace events are safe to surface in
  dashboards; the `RECALL` step-prompt doc block does NOT touch the
  planner prompt's `## Issue` / `## Retrieved context` markers your
  difficulty estimator keys on.

## Product round 2026-09-24 — skill receipts and prompt evidence

- `harness/skills.py` now rejects malformed/binary skill files honestly,
  supports platform/config-aware global roots, retains plugin disabled
  markers, and returns `receipts`, `rendered`, `omitted`, and diagnostics.
  Match reasons identify issue, retrieval, and repository terms. The
  selected skill block itself carries origin, source, and matched terms, so
  the receipt reaches the planner's actual model message and trace.
- The existing verifier-gated `run_task` planner path remains covered by
  real-loop content receipt. `tests/test_skills.py` is 31 passed, including
  plugin-origin matching and the trace assertion.
- Prompt 2 integration is complete: `harness/agent_loop.py` now calls
  `scan_skills_for_task` before the general agent's first model request,
  injects the bounded skill block after retrieved context, emits the `skills`
  receipt, and keeps the block in the live message sequence across steering.
  The strict adapter carries the same block through session metadata.
- The deterministic daily skill probe passes both arms, and the focused
  legacy-loop suite remains 61/61 green.
- Required-suite result: exit 0, 149 passed, 1 skipped; the only skip was
  the Windows state-file symlink privilege case, not Docker. Ruff check
  passed on all owned changed Python files.

## Product round security follow-up 2026-09-24

- `harness/skills.py` now refuses symlinked roots/entries, reads at most
  64 KiB per skill file, retains the legacy `~/.config/neo` global location
  after the platform-aware root, and matches only the repository directory
  name rather than ancestor/home path components.
- The new non-loop skill coverage passes 30 tests with one Windows symlink
  skip. The three real-loop receipt tests pass in the documented
  `HARNESS_EXEC_SKIP_DOCKER=1` lane; the plain lane stops at baseline
  verification because the Docker daemon is unavailable on this host.

## Terminal 1 verifier-loop audit (2026-09-24)

- T1-owned harness hardening is implemented across dependency resolution,
  context budgeting/reranking, planner validation, edit/path safety, trace
  redaction, web-fetch guards, steering locking, build/scan handoffs, and
  verifier evidence checks. Explicit stub fallback is opt-in through
  `HARNESS_USE_STUBS` or `HARNESS_ALLOW_STUB_FALLBACK`; imports failing inside
  a real boundary are never hidden.
- Docker-free focused evidence: the 357-test T1 selection returned 345
  passed, 11 skipped, and one live-PyPI content failure; the 263-test core
  selection returned 248 passed and 15 Docker/platform skips. The
  Docker-free security selection returned 63 passed and 31 skips.
- Real Docker evidence: `tests/test_e2e_run_task.py` returned 28/28 passed;
  the real core selection returned 178 passed and 2 failed; adversarial
  security returned 44/44 passed; skills returned 33 passed and 1 platform
  skip. Real build-plan failures are caused by T2's verifier helper treating
  the substring `0 passed` inside `10 passed` as no tests; T1 did not mask
  that evidence.
- Prompt-regression host check returned 14/14 CLEAN. The quick matrix is
  currently ERROR because the parallel eval runner probes one directory
  above the actual nested task trace; T4 owns that path correction and must
  rerun the matrix. Live PyPI content tests are blocked by the current
  anti-bot/network response.
- Final scoped `ruff check` and `git diff --check` pass. `graphify update .`
  rebuilt 12,835 nodes, 28,363 edges, and 2,669 communities.

### T1 integration requests

- T2: tokenize pytest's passed-test count in `execution.verify._result_passed`;
  substring matching currently rejects legitimate counts such as 10 and 20.
  Keep the harness verifier gate strict until that boundary fix lands.
- T4: correct the eval runner's nested `arm/task/task/trace.jsonl` lookup,
  then rerun the prompt matrix; do not treat the current quick ERROR as a
  prompt regression.
- T4: document the explicit harness stub opt-ins and preserve the new
  dependency-resolution behavior in the release contract.

## VEX-ARCH-03 — cited context engine, repository map, and LSP (2026-09-25)

- `harness/context_compiler.py` is the shared compiler. It discovers root-to-
  target `.neo` instructions plus `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, and
  `QWEN.md`; keeps them separate from decision memory and skills; protects
  project instructions and acceptance criteria under tiny token budgets; and
  returns stable citations, lossless source references, role/provider budgets,
  digest-aware cache receipts, and reversible compaction. `harness/context.py`
  re-exports the compiler without changing the Boundary-4 `state.json` prefix.
- `harness/retrieval.py` now exposes deterministic weighted-PageRank symbol
  ranking, repository maps, exact symbol/range reads, caller/callee/import
  blast-radius context, content/index digests, and opt-in citations while its
  historical four-key `retrieve_context` result remains compatible.
- `harness/lsp.py` is an optional stdlib JSON-RPC lifecycle with binary-safe
  framing, diagnostics, hover, document/workspace symbols, references,
  bounded requests, restartable shutdown, and explicit unavailable/timeout
  degradation. Its public lifecycle is `start`, `open_document`,
  `update_document`, `get_diagnostics`, and `shutdown`; paths are contained
  under the workspace root. No language-server package or live provider is
  required. The AST lint path remains `harness.lint` and is not an LSP alias.
- `memory/code_graph.py` now persists per-file content digests, detects
  same-size edits with preserved mtimes, gives mixed Python/JS/TS symbols and
  modules collision-safe IDs, keeps Python legacy IDs stable, and exposes
  exact/source/digest helpers. Call-graph resolution remains the documented
  name-based over-approximation.
- Regression coverage is in `tests/test_retrieval_tools.py`,
  `tests/test_code_graph.py`, `tests/test_multilang_graph.py`, and
  `tests/test_lsp.py`. The exact required suite passed 94 tests with two
  platform skips; the focused suite including LSP passed 100 with the same
  skips. The skips are symlink/platform cases, not Docker or provider passes.
  The daily-driver public-LSP probe is covered by
  `tests/test_daily_driver_evals.py` and reports
  `lsp_diagnostic_repair_receipt` only after a real subprocess diagnostic
  is observed, consumed, repaired, and cleared.
- Follow-up hardening keeps every compiler source normalized, redacted, and
  referenceable: task, instruction, skill, memory, turn, LSP, and dependency
  sources carry stable digests/citations and `source_ref` records; instruction
  byte-cap truncation is explicit; implicit skill discovery is included in
  the cache request. Repository-relative inputs reject traversal, absolute
  escapes, and symlink components. Empty/invalid roots never scan the process
  working directory.
- `LspManager` enforces workspace containment for document/query methods and
  treats an explicit empty pull-diagnostics response as authoritative; stale
  notification diagnostics are used only when a request is unavailable.
- `Graph.canonical_defines` is the node-id-only define-edge contract;
  historical qualified aliases remain in `Graph.defines` for compatibility.
- Configuration values are read from `Task.config`/compiler config:
  `context_token_budget`, `context_chars_per_token`,
  `context_provider_chars_per_token`, `context_role_weights`,
  `context_provider_weights`, `context_map_symbols`,
  `context_dependency_limit`, `context_recent_turns`, `context_cache_entries`,
  `skills_enabled`, `skills_max`, `skills_max_chars`, `skills_roots`, and
  `lsp_timeout_s`. Provider weight mappings are role-relative; a scalar
  provider weight is normalized as a uniform multiplier.
- Honest integration boundary: the legacy `harness.core`/`harness.agent_loop`
  callers still need an owner wiring `ContextCompiler` and
  `emit_context_trace` into their model requests. The compiler is complete and
  callable now, but normal historical retrieval traces remain unchanged until
  that cross-module wiring lands. No Docker or live-provider lane was run for
  this optional LSP/context work.

## VEX-TERM-UX-08 cancellation cleanup (2026-09-25)

- The real-PTY cancel scenario found that a TUI async `KeyboardInterrupt`
  could return control while a local descendant process was still alive.
  `harness/agent_kernel/kernel.py` now cancels the execution backend's side
  effects and calls `execution.workspace.cleanup_active_processes()` during
  kernel teardown, including the interrupt path.
- `tests/test_agent_kernel.py` remains green at 38 passed. The end-to-end
  evidence is `logs/terminal-ux/terminal-08-evidence/20260925-164517/cancel`,
  which asserts the long command started, `/cancel` returned control, the
  descendant was gone (zombie state treated as cleaned), and the session
  remained resumable.
- This is cleanup of processes owned by the current worker only; it does not
  change verification, completion status, or any public contract.
## VEX-CEILING-05 — context compiler, symbols, LSP, memory capture (2026-09-26)

**The four knowledge capabilities this repository already had are now on the
one path the daily agent actually runs.** Before this round the compiler, the
symbol index, and the LSP existed and were exercised only by the eval probe
and the CLI viewer; the daily strategy never touched them. That is now closed,
and the OFF arm is a single config key.

### NEW `harness/knowledge.py` — the run-scoped binding

`KnowledgeContext` owns exactly one `ContextCompiler`, one `LspManager`, and
one `DecisionStore` per run and exposes the whole surface the strategy needs.
It implements nothing itself; every capability is delegated. Public
signatures and the full guarantee list are in `INTERFACES.md` under
"Ceiling Terminal 05 — knowledge binding". Read that before changing any
method: the honest-absence and never-raise rules are the contract, not
incidental.

**Compile once per run, inject into EVERY request.** `compile()` is
idempotent; the second call returns the same receipt with `memo_hit: True`.
The daily strategy seeds its `[system, user]` frame ONCE per run
(Terminal 01's `ConversationMemory`), so appending the compiled block to
`_metadata_context` makes it a **prefix of every later model request** rather
than something a later turn re-derives. That is why "compile once" and
"inject everywhere" are consistent rather than in tension.

**Reversible compaction metadata** rides the `context` trace event:
`compaction_metadata` carries `compacted`, `omitted`, `source_reference_count`,
`citation_count`, `cache_key`, `source_digest`, and `index_digest`. A compacted
bundle still names every source it dropped, so the dropped text is
recoverable from `source_references` rather than gone.

### Symbol-level tools (now in the ONE canonical catalog)

`read_symbol`, `find_definition`, `find_references`, `blast_radius` are
`read_only` entries in `harness.tools._TYPED_TOOL_SPECS`, so the kernel
derives them automatically and `catalog_parity_report` covers them. Handlers
live in `build_default_handlers` and resolve through `context["knowledge"]`.

**The <=3-call reachability contract, and why it holds.** A model starts with
no knowledge of a file it has not read. `find_references` returns the symbol's
definition *and* its callers/importers with citations; `read_symbol` then
returns the definition's real source. Two calls, no repository scan, no
`grep` over 24 distractors — pinned by
`test_symbol_outside_the_initial_window_is_reachable_in_three_tool_calls`,
which asserts the absence of the distractors in the TOOL RESULT (the compiled
repository map legitimately names modules; that is a budgeted context section,
not a scan).

**Honest absence is the point.** When the index cannot be read, the four tools
say the index is unavailable and say explicitly that this is NOT a claim that
the symbol does not exist. `find_references` and `blast_radius_for` report
`resolution: "name_based"`; the answer is "files to CHECK", not proof of
breakage. `read_symbol` falls back to a bounded text search and labels the
record `source="text_search"`.

**`memory_record` is permission-gated** (`approval=True` in the catalog, so
approval is required before `memory_record_enabled` is even consulted). The
handler routes every write through `shared.security.authorize_memory_write`
*before* the store: a row claiming system authority is quarantined, a
credential-shaped row is refused. Text asserting an unverified outcome
("all tests pass now") is stored as `category="observation"` with
`claim_downgraded` set, so a later session reads it as a report, not a fact.

**Dedupe had to move out of the store.** `DecisionStore.record(dedupe=True)`
is keyed on `(task_id, repo_path, text)` — scoped to ONE task. A convention
recorded by a later SESSION would therefore have landed a second row.
`KnowledgeContext.find_recorded` does a repository- and category-scoped
normalized-text check *before* insert and reports the FIRST record's id, which
is exactly the "a later session demonstrably reuses the record" proof.

### Language-server feedback on the daily path

`lsp_enabled` defaults **False** (it starts a real process). When on:
`LspManager.from_config` builds the manager, `sync_document` sends `didOpen`
for a path's first sync and `didChange` afterwards, and after every successful
mutation the strategy drains diagnostics and appends them to the next model
turn as the LAST message — so a real compiler's finding outranks the model's
optimism about its own edit. A cleared report is stated explicitly
("the file you just changed now reports no findings"), which is how a repair
is observed to have cleared a finding. Events:
`lsp_diagnostics`, `lsp_diagnostics_observed`, `lsp_observation_failed`,
`knowledge_close`. A missing binary, a timeout, or a broken handshake is a
normal outcome reported in `lsp_status`, never a run failure.

**Two real defects this wiring found, both fixed and regression-pinned:**
1. `LspManager.from_config`'s second parameter is `repo_path`, not `cwd`;
   passing `cwd=` raised `TypeError` and silently disabled diagnostics.
2. `dict.fromkeys(targets)[:8]` — slicing a dict raises
   `unhashable type: 'slice'`, which propagated out of `_execute_calls` and
   turned a GOOD edit into `failed`. `_observe_lsp_after_mutation` is now also
   wrapped in a `try/except` at its call site: an enhancement must never
   change a run's outcome.

`dd_26_lsp_diagnostic_repair` was already green through the public
`harness.lsp` lifecycle before this round; what was missing was the DAILY PATH,
which is what closed here. The AST lint path (`harness.lint`, `lint_gate`,
`lint_failed`) is untouched and is NOT an LSP substitute.

### Retrieval ablation (lexical vs hybrid) — `retrieval.retrieval_ablation`

The production ranking is **lexical and structural** (identifier, subword,
camelCase decomposition over the tree-sitter index) and is documented as using
no embeddings. An embeddings arm exists **only as this ablation**: the same
queries are scored by the production lexical scorer and by a hybrid scorer that
adds a bounded character-ngram term. It calls no model and opens no socket, so
it is a deterministic measurement probe, not an embedding service. It reports
`lexical_recall_at_k`, `hybrid_recall_at_k`, `hybrid_minus_lexical`, and
per-query agreement. Recall is computed only for LABELED queries; an unlabeled
query reports its ranking and recall stays `None` rather than becoming a
fabricated `0.0`. `_label_chain` expands qualified labels into dotted suffixes
so a human-written `Widget.render` matches a node id of
`method:pkg.core.Widget.render` — matching exact ids only would have reported
recall misses for queries that actually ranked the right symbol first.

### New config keys (`harness/config.py`, all defaulted)

`knowledge_enabled` (True — the single OFF arm), `knowledge_block_max_chars`
(24000), `knowledge_tool_max_chars` (6000),
`knowledge_diagnostics_max_chars` (2000), `context_token_budget` (12000),
`context_chars_per_token` (4), `context_cache_entries` (32),
`context_recent_turns` (6), `context_map_symbols` (25),
`context_dependency_limit` (20), `decision_memory_enabled` (True),
`lsp_enabled` (False), `lsp_timeout_s` (5.0), `memory_record_enabled` (True).

The compiler's config keys were previously read but had no defaults, so a
default run had no budget shape at all. They are now real.

### Cross-module edits made by this round (disclosed)

- `harness/tools.py` (Terminal 04's canonical catalog): five additive
  `_TYPED_TOOL_SPECS` entries. No existing entry was modified. The catalog
  stays ONE list and the kernel still derives from it.
- `harness/agent_kernel/strategy.py` (Terminal 01/02's file): additive methods
  only — `knowledge()`, `_knowledge_event()`, `_compile_knowledge()`,
  `_observe_lsp_after_mutation()`, `_canonical_mutation_tools()`,
  `_close_knowledge()`; `_metadata_context` gained a compiled-block append;
  `_handler_context` gained a `knowledge` key; `_execute_calls` gained the
  post-mutation LSP note; `build_default_handlers` gained five handlers. The
  `context` event is emitted once per cache key, and `knowledge_bound` exactly
  once per run.
- `mcp_server/server.py` (this terminal's Boundary 5 surface): additive
  optional parameters on `record_decision` only.
- `INTERFACES.md`: Change Log entry plus the public contract block above.

### Verification actually run

- `python -m pytest tests/test_ceiling05_knowledge.py -q -p no:randomly` ->
  **27 passed** (new suite: all six required proofs plus the OFF arm,
  per-turn injection, honest-absence, gate/degradation, catalog shape,
  provenance, claim downgrade, and rendering contracts). The language server
  is a real stdlib JSON-RPC child process, so the diagnostic proof exercises
  the real transport.
- `python -m pytest tests/test_ceiling05_knowledge.py
  tests/test_agent_kernel.py tests/test_tool_protocol.py
  tests/test_workspace_security.py tests/test_lsp.py
  tests/test_retrieval_tools.py tests/test_code_graph.py
  tests/test_mcp_server.py tests/test_mcp_adversarial.py
  tests/test_mcp_client.py tests/test_decision_store.py
  tests/test_multilang_graph.py -q -p no:randomly` -> **326 passed, 3
  skipped** (skips are Windows symlink-privilege cases).
- `python -m pytest tests/test_agent_loop.py
  tests/test_config_trace_state.py tests/test_stubs_and_deps.py -q -p
  no:randomly` -> **86 passed**.
- `python -m evals.run --suite prompt-regression --check` -> **14/14 CLEAN**.
- `python -m ruff check` clean on every owned and disclosed file;
  `python -m compileall -q harness mcp_server` clean; scoped
  `git diff --check` clean.
- `harness/knowledge.py` and `tests/test_ceiling05_knowledge.py` are fully
  `ruff format` clean. `harness/retrieval.py`'s added region (lines
  1833-2525) is format-clean; the remaining `ruff format` debt in
  `retrieval.py`, `tools.py`, `config.py`, `mcp_server/server.py`, and
  `agent_kernel/strategy.py` is PRE-EXISTING whole-file debt in this shared
  dirty tree and was deliberately not swept, per the same convention
  Terminals 01 and 04 used.

### Concurrent-work observation (important for whoever reads this next)

`harness/agent_kernel/strategy.py`, `context.py`, `budget.py`,
`conversation.py`, and `__init__.py` were being rewritten by Terminal 02
(session continuity / context budget / compaction) DURING this round, with
mtimes within minutes of the work. One regression run transiently showed 6
`tests/test_agent_kernel.py` failures with
`ContextBuilder.build() got an unexpected keyword argument 'turn'`; that was
their half-written save, not a defect here — a clean re-run was 47/47. **This
file is a shared editing target; re-run `tests/test_agent_kernel.py` before
attributing a failure to the knowledge layer.**

### Not yet implemented / honest blocked status

- **`dd_26` daily-driver capability is NOT re-closed by this round.** The case
  was already green through the public `harness.lsp` lifecycle; this round
  closed the DAILY PATH, which is a different claim. The
  `lsp_diagnostic_repair` capability gate in `evals/daily_driver.py` is
  Terminal 10's and still needs a re-run to be counted.
- **Two `tests/test_daily_driver_evals.py` failures are PRE-EXISTING and not
  caused by this round.** `test_feature_evidence_lane_covers_every_active_feature`
  and `test_quick_matrix_runs_real_comparison_with_receipts` fail with
  `status: failed, pass 6, fail 20` — every feature reporting
  `core_run_succeeded: false`. **Proven, not assumed:** re-running the feature
  lane with this round's five catalog entries removed AND
  `harness.knowledge` made unimportable produced the byte-identical result
  (6 pass / 20 fail). The failing lane runs through `harness.core.run_task`
  (the legacy path), which imports none of this round's code and reads none
  of its config keys. This is the same legacy-completion defect Terminal 04's
  handoff recorded for `dd_02`/`dd_04`/`dd_19`, in Terminal 01's in-flight
  `legacy_agent` work. Not counted as a pass.
- **The hallucinated-symbol-rate target (<2% on a labeled sample) is NOT
  MEASURED.** The mechanism that prevents hallucination is pinned (absent
  symbols are reported absent, never invented, and an unreadable index is
  reported as unknown), but this round ships no labeled sample and no rate
  number. Claiming a rate here would be a fabricated metric.
- **The "files re-read per successful task decreases measurably" target is
  NOT MEASURED.** Doing it honestly needs a before/after corpus of successful
  runs with per-task read counts; this tree has no such baseline, and the
  runs available are Terminal 10's, mid-flight. The mechanism (citations and
  digests on every symbol record, plus reference-before-read) is built and
  tested; the measured delta is not claimed.
- **No live-provider lane was run and no credential was inspected, requested,
  or retained.** Every provider-shaped assertion uses a scripted callable.
- **No Docker-backed verifier lane was run by this round.** All evidence above
  is host-only and Docker-free. The daily-driver matrix was not re-run.
- The hybrid ablation arm is a deterministic character-ngram probe, NOT a
  real embedding model. It measures whether a similarity term changes recall
  on a labeled set; it does not measure what a production embedding index
  would do.
- A real external language server (pyright, pylsp) was never launched. The
  proof uses a stdlib JSON-RPC fixture subprocess, which exercises the real
  transport, framing, lifecycle, and diagnostic normalization, but is not a
  claim about any specific analyzer's accuracy.

### Integration requests

- **Terminal 01/02 (`harness/agent_kernel/`):** `strategy.py` is now a
  three-way shared file. The additive knowledge methods are documented above;
  please keep them if you rebase. `_metadata_context` appending the compiled
  block is the single injection point for compiled context, skills, and plan
  guidance.
- **Terminal 04 (`harness/tools.py`):** the catalog is now 43 tools. All five
  additions are `read_only` except `memory_record`, which is `memory` +
  approval-gated. `catalog_parity_report` covers them automatically; the
  handler-existence test in `tests/test_tool_protocol.py` still passes.
- **Terminal 10 (`evals/`):** the two failing `test_daily_driver_evals.py`
  tests are in your lane and are not caused by this round (proof above). The
  `lsp_diagnostic_repair` capability is now also exercised on the DAILY PATH,
  so a re-run of `dd_26` with `lsp_enabled=True` in the arm config would be
  genuine new evidence rather than a probe re-run.


## VEX-CEILING-12 - versioned skill declarations (additive, 2026-09-26)

Skill now carries four extra **inert** fields parsed from SKILL.md
frontmatter: ersion (positive int, 1 when unusable), model_tier
(model-tier / model_tier), permission_claims (permissions), and
declared_tools (	ools). The frontmatter parser also accepts a YAML **block
sequence** under permissions: / 	ools: (previously only a single-line or
JSON value), so the common authoring form works. _declaration_items accepts a
JSON list/object, a YAML block sequence, a YAML flow list, and a comma/semicolon
CSV, and returns () for anything else.

Two additive receipt keys make the declaration auditable without changing the
existing ones: each entry in _ranked_matches's receipt gains a declaration
object (the **declared** data), and uild_skill_receipt gains a
declarations summary keyed by skill name.

**These fields grant nothing.** Enforcement lives in
extensions.skill_policy.resolve_permissions /
resolve_tools /
clamp_tier, which intersect a declaration with the parent session's
permission envelope. A skill can declare any tool, permission, or model tier it
likes; nothing in harness/skills.py applies one. That separation is the
security property - a prompt-layer module is not where a grant belongs.

Backward compatibility: the four new Skill.__init__ parameters are
keyword-defaulted, so every existing caller and test keeps working unchanged,
and Skill.tainted / the untrusted-content review are untouched.

	ests/test_skills.py re-runs green (33 passed, 1 Windows symlink skip,
including the three REAL-loop Docker e2e). New declaration/permission coverage
lives in 	ests/test_ceiling12_hooks.py.

**Not wired:** nothing calls skill_policy from the planning path yet. The
kernel/loop owner should build a SessionPermissions from the run's own policy
rules and pass it to extensions.skill_policy.explain_selection(scan, ...) at
plan time so the skills trace row carries the effective tier, the intersected
permissions, and every refused claim. Recorded in
logs/ceiling/terminal-12.json.

## VEX-CEILING-R2-03 — test CONFIGURATION is protected, and its EFFECT is measured (2026-09-26)

**Group 0a. Owns `harness/editor.py` + the new `harness/test_config.py` + `tests/test_ceiling_r2_03_config_guard.py`. Did NOT touch `harness/core.py`, `harness/agent_loop.py`, `execution/verify.py`, or `harness/config.py`.**

**The hole (F4, verified in the tree before this round):** `conftest.py`,
`pytest.ini`, `tox.ini`, `setup.cfg`, and `[tool.pytest.ini_options]` were
absent from the protected set, so an agent could deselect the failing test and
the regression gate would report a clean run. The verifier answers "did the
suite pass"; nothing asked "is this the same suite".

### 1. Three layers, not one

| Layer | Where | What it decides |
|---|---|---|
| protected **surface** | `test_config.classify_test_config_path` / `editor.is_protected` | which paths/tables are part of the test contract |
| **declared intent** | `test_config.declared_test_config_change` | whether THIS run may change test configuration, and records why |
| **effect** | `test_config.resolve_effective_test_config` + `test_config_guard` | whether the configuration actually in force differs from the baseline's |

### 2. The protected set, per language, with table scoping

Whole-file surfaces (protected in `is_protected`, independent of
`protected_paths`): `conftest.py` (ANY directory — pytest's own scoping),
`pytest.ini`, `sitecustomize.py`, `usercustomize.py`, and the runner configs
for js (`jest.config.*`, `vitest.config.*`, `karma.conf.*`,
`playwright.config.*`, `cypress.config.*`, `.mocharc*`, `mocharc.*`), ruby
(`.rspec`, `spec_helper.rb`, `rails_helper.rb`), php (`phpunit.xml*`),
dart (`dart_test.yaml`), go (`go.test.conf`), .net (`xunit.runner.json`,
`nunit.config`, `*.runsettings`).

**Table-scoped** surfaces (dual-purpose files — only the test tables are
protected, and the check needs the tree, so it lives in `check_edits`):
`pyproject.toml` → `tool.pytest.ini_options`; `setup.cfg` → `tool:pytest`;
`tox.ini` → `pytest`; `package.json` → `jest`, `scripts.test`; `angular.json` →
`test`; `pom.xml`/`build.gradle*` → `test`.

Consequences worth knowing:
- **Editing `[project] dependencies` in `pyproject.toml` is still a normal
  edit**; only its pytest table is protected. Pinned both ways in the suite.
- `editor.extended_protected_patterns(...)` /
  `editor.test_config_patterns(language)` exist so a consumer EXTENDS its
  `protected_paths` list instead of forking a policy.
- `test_config.register_language_surfaces(lang, mapping, replace=False)` is the
  seam R2-12 (polyglot) should use for Go/Rust/Cargo rather than editing this
  module's tables. **`Cargo.toml` is deliberately NOT a surface** (its
  `[dev-dependencies]` is test config, but a whole-file lock would block every
  legitimate dependency edit) — a stated gap, not an oversight.

### 3. The declared intent — `Task.config["test_config_change"]`

A task whose declared purpose is to change test configuration sets
`test_config_change` to a reason (a string, or `{"reason": ...}`) and the
change proceeds. Rules, all pinned:
- **Key presence, never issue prose.** Absent / `None` / `False` / `""` = not
  declared. Issue text is reachable by the same agent the guard constrains, so
  it cannot be an authorization. (`harness.core._authorized_test_target`
  matches issue prose for a different, narrower exception; this deliberately
  does not copy that part, and says so in the docstring.)
- The key is **deliberately NOT in `harness/config.py` `DEFAULTS`**: a default
  merged into every task would silently switch every run. A `None` entry may be
  added there for discoverability — `declared_test_config_change` already
  treats `None` as not-declared, so that addition is behavior-neutral.
- A declared change is a **RECEIPT, not a silent bypass**: it still emits
  `test_config_changed`, with the relaxations it introduced.
- **There is deliberately no key that DISABLES the guard.** A refusal cannot
  be configured away, only declared.
- Measured trade, pinned: a declaration also clears the *configured* glob for
  exactly those files (so `tests/conftest.py` under `protected_paths=["tests/*"]`
  is editable when declared). It does not unlock anything else — an ordinary
  `tests/test_x.py` edit is still refused under the same declaration.

### 4. The effect — the part that is actually new

`resolve_effective_test_config(root, target=, extra_paths=, test_command=)`
resolves, WITHOUT running pytest: the inifile by **pytest's own precedence**
(`pytest.ini` wins by existence; `pyproject.toml` only when its table is
present; then `tox.ini`, `setup.cfg`), the normalized options, the tokenized
`addopts`, the `conftest.py` chain that can reach the declared target test, and
any config found up to 4 levels ABOVE the root. Everything is digested into one
`sha256` over a canonical JSON projection.

`test_config_guard(pristine, work, config=, changed_paths=)` resolves both
trees, diffs them, and returns `TestConfigVerdict(ok, message, receipt,
violations, allowed_paths)`. It refuses when a surface changed with no
declaration, OR when the effective resolution differs for a reason no surface
explains (the prompt's hard case: **a `pytest.ini` above the tree**, where no
repository file changed at all). The receipt names the relaxations
(`addopts_selector`, `addopts_ignore`, `addopts_plugin`, `addopts_deselect`,
`addopts_ini_override`, `addopts_collect_only`, `addopts_last_failed_only`,
`scope_narrowed`, `inifile_changed`, `conftest_added|removed|modified`,
`sitecustomize_*`, `pyproject_pytest_modified`, …) plus the full before/after.

**Cost: O(changed paths + directory depth), never a repository walk.** The
`conftest` chain is walked from the declared target's directory upward, and
every other surface comes from the caller's changed set (which the edit gate
already computed). `conftest_scope` records whether the chain was
`target_chain` or `root_only` so a partial resolution can never read as a
complete one. R2-G16/G17's scale traps are not re-entered here.

### 5. What is live NOW vs what is a hand-off

**Live, no other file touched:** `check_edits` gained three keyword-only
params (`config`, `trace`, `report`) and calls the guard BEFORE the
configured-glob loop. `run_task` calls `check_edits` at `harness/core.py:1291`
and `:2659` with three positional args, so **the refusal is already on the
production path** — proven end-to-end (see §7). `is_protected` refuses
whole-file surfaces, so `harness/agent_loop.py`'s EDIT/WRITE refusals
(`agent_loop.py:2238`, `:2272`) and its BASH change audit
(`agent_loop.py:1697`) also cover them today.

**Hand-off (requested, not done — those files are not mine):**

1. `harness/core.py:1291` and `:2659` — pass the sink so the RECEIPT reaches
   the trace and the task result:
   ```python
   ok_edits, edit_msg, _ = editor.check_edits(
       str(paths.pristine), str(paths.work), protected,
       config=cfg, trace=trace, report=task_test_config_receipts,
   )
   ```
   Then emit `test_config_changed` (or fold the receipt into the result) in the
   refusal branch at `core.py:1294-1302`. Both params are additive and
   keyword-only; omitting them keeps today's behaviour.
2. `harness/core.py:608-612` and `:2479-2483` — those two sites strip the test
   globs when `_authorized_test_target` is true. They should also skip the
   test-config surfaces when `editor.declared_test_config_change(cfg)` is
   truthy, so a declared task is not refused at the tool layer.
3. `harness/agent_loop.py:2238`/`:2272` — same declaration check in the
   interactive EDIT/WRITE refusals (they have `cfg` in scope at `:1361`/`:2093`).
4. `harness/config.py` — add `"test_config_change": None` to `DEFAULTS` for
   discoverability (behavior-neutral, see §3). No other config key is needed
   and no default may enable the gate.
5. `cli/main.py:114` / `:1102` — `extended_protected_patterns(protected,
   language=...)` if the CLI wants the whole-file surfaces in its own
   `--protected` list for early, pre-run feedback.

### 6. Honest failure policies (both pinned by tests)

- A guard that RAISES degrades to **non-blocking** + a recorded
  `test_config_guard_failed` row: an unrelated `OSError` must not turn a
  correct fix into a failure. The receipt is NOT written in that case, so a
  reader is never told "no test-config change" on the strength of a crash.
- A guard that **resolves nothing** (unreadable/missing tree) still refuses a
  file-level violation, because that verdict needs no resolution. With no
  violation it passes with `resolved: false` and the reason in `error`.

### 7. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_ceiling_r2_03_config_guard.py` → 78 passed** (77 host +
  1 real Docker). The four required proofs: conftest tamper refused by the edit
  gate; `pytest.ini` tamper refused; a declared change proceeds AND appears in
  the receipt (with before/after and its relaxations); an effective-config
  difference with no declaration reports `test_config_changed`. Plus the
  inifile-precedence matrix, the conftest-chain/`conftest_scope` contract, the
  per-language classification matrix, the relaxation vocabulary, the
  degradation policies, and a "no over-blocking" matrix.
- **The real-loop proof:** `test_run_task_refuses_a_vacuous_green_via_a_
  conftest_tamper` drives the REAL `harness.core.run_task` with a scripted
  model, REAL Docker sandbox and REAL verifier. The scripted "fix" writes
  `collect_ignore_glob = ["test_mathutil.py"]` into a new `conftest.py` and
  submits. Result: **`failed`**, with `final_edit_validation_failed` naming
  `conftest.py` and the test configuration — the attempt is poisoned BEFORE
  the final verify, and the fixture repo is untouched. The same test asserts
  the *honest* current limit: the run's trace has the refusal but not yet the
  receipt, because `run_task` passes no sink. The sink itself is proven against
  the real `harness.trace.TraceLogger` in the next test, so the hand-off is
  one line.
- `tests/test_e2e_run_task.py` (real Docker sandbox + verifier) → **28 passed**
  in 500.75s. The edit-policy change did not disturb the real gate.
- Neighbour suites for the shared file: `test_editor_prompts`,
  `test_adversarial`, `test_config_trace_state`, `test_coordination`,
  `test_multilang_graph`, `test_workspace_security` → **198 passed, 1 skipped**
  (the skip is a Windows symlink-privilege case).
- `test_ceiling_security`, `test_skills`, `test_decision_memory_planning`,
  `test_daily_driver_evals`, `test_evals_tasks` → **132 passed, 1 skipped**.
- `python -m evals.run --check` → **14/14 CLEAN**.
  `python -m evals.run --quick` → **40/40, verdict CLEAN, 0 regressions** (5
  tasks × 8 arms through the real loop on fixture repos that all carry a
  `pyproject.toml` pytest table — the guard does not fire vacuously).
- `ruff check` clean on `harness/editor.py`, `harness/test_config.py` and the
  test file; `ruff format --check` clean on the two NEW files. **No formatter
  was run on `harness/editor.py`** (shared file, protocol §4.2.3).
  `python -m compileall -q` clean; `git diff --check` clean.

**Not run:** the full Docker prompt matrix (`python -m evals.run` without
`--quick`) and any live-provider lane. Neither was selected and neither is
claimed. No credential was inspected.

### 8. Not implemented, stated plainly

- **The resolver reads configuration; it never runs the test runner.** It
  cannot observe what pytest would actually collect, so it is evidence about
  the declared configuration, not a substitute for a run. R2-12's zero-test /
  JUnit work is where a collection observation belongs.
- **No repository-wide `conftest` scan.** Detection of an added conftest relies
  on the caller passing the changed set. A conftest that existed before the run
  and is outside the declared target's directory chain is recorded in
  `conftest_scope` as *not* covered rather than being searched for.
- `Makefile`, CI workflow files, `.pre-commit-config.yaml` and `noxfile.py` are
  NOT treated as test configuration. They can run tests, but protecting them
  would block ordinary CI edits; that is a judgement call, and it is stated
  rather than hidden.
- `package.json`'s `scripts.test` is read through the same tolerant section
  reader, not a JSON parse, and `"scripts": {"test:unit": ...}` (a colon) is
  not a key this table protects.
- A declaration clears the configured glob for the files it covers (§3). If
  that is judged too wide, the narrowing belongs in the hand-off at
  `core.py:608-612`, not in the guard.
- The receipt is not yet part of `TaskResult` or `RunResult` (hand-off §5.1).


## VEX-CEILING-R2-07 - the AST codemod layer: a plan, never an action (2026-09-26)

**Group 0a. Owns NEW `harness/codemod.py`, additive `_TYPED_TOOL_SPECS`
entries in `harness/tools.py`, and one additive handler plus two
registrations in `harness/agent_kernel/strategy.py`. Did NOT touch
`harness/config.py`, `harness/core.py`, `harness/agent_loop.py`,
`harness/editor.py`, `execution/**`, `memory/**`, `runtime/**`, or any
verifier. The success mint is untouched and no completion status changed.**

### 1. The problem this closes

Renaming a symbol or changing a signature means reading and editing every
call site by hand, through the model, one turn at a time. On a real codebase
that is dozens of turns and dozens of chances to miss one - and a missed call
site is not a failed task, it is a task that **looks** fixed.

### 2. `harness/codemod.py` - two operations, and a plan is the only output

`rename_symbol(repo_path, old_name, new_name, *, path=None, config=None,
index=None)` and `update_signature(repo_path, symbol, *, path=None, added=(),
removed=(), renamed=None, retyped=None, config=None, index=None)` both return
a `CodemodPlan` and **change nothing**. A plan carries:

- `edits: tuple[PlannedEdit, ...]` - one exact `old_string` / `new_string`
  pair per source line, already addressable for the ordinary `edit` tool, plus
  the file's `expected_sha256`;
- `sites: tuple[CodemodSite, ...]` - every resolved occurrence with its
  `path` / `line` / `column` / `kind`;
- `receipt: CodemodReceipt` - the completeness receipt (below).

Public surface: `SCHEMA_VERSION`, `OPERATIONS`, `RELIABLE_LANGUAGES`, the
`SITE_*` / `UNRESOLVED_*` closed sets, `CodemodConfig`, `config_from`,
`CodemodSite`, `UnresolvedSite`, `PlannedEdit`, `CodemodReceipt`, `CodemodPlan`,
`CodemodApplyResult`, `CodemodOutcome`, `language_of`, `language_support`,
`rename_symbol`, `update_signature`, `plan_codemod`, `apply_plan`,
`run_codemod`, `render_plan`, `render_receipt`. One `plan_codemod(operation,
repo_path, symbol, new_name, *, ...)` is the single dispatch point, and
`run_codemod(...)` is the one-call plan-then-apply path.

### 3. Two existing extractions, and no second parser

- **Which symbol, and which module** - `memory.code_graph.CodeGraph`
  (`exact_symbols`, the `module` node for a file). The project's one
  structural index; imported lazily so a missing memory layer degrades to an
  honest refusal instead of an import error.
- **Which line and column** - stdlib `ast`, the same parser
  `runtime.symbols._python_symbols` and `harness.lint` already use for Python,
  plus stdlib `tokenize` for the completeness pass. No third grammar, no
  regular-expression "parser", no duplicate index.
- **Which languages are reliable** - `runtime.symbols`' own suffix sets.
  Python (`.py`, `.pyi`) is the only entry in `RELIABLE_LANGUAGES`.

`SKIP_DIR_NAMES` is defined **locally** in `harness/codemod.py` rather than
imported from `memory.code_graph`: `memory` is another module's internals, and
a hard dependency on its private skip list would make a rename's scope depend on
that module's refactors. Same for `_module_name` / `_from_import_module`, which
re-implement the same four-line path-to-dotted-name convention.

### 4. Python is resolved PRECISELY, not by name - this is the load-bearing part

The first version used `memory.code_graph`'s documented name-based
over-approximation and **rewrote an unrelated same-named function in another
module**. A rename of a common name must not do that, so each candidate file
is classified once (`_FileRole`) and every occurrence is judged against it:

| occurrence | a SITE only when | otherwise |
|---|---|---|
| definition | it is in the defining module | NAMED `other_symbol_with_same_name` (advisory) |
| bare `Name` | the file is the defining module, or imported the symbol from it | NAMED `other_reference` (**blocking**) |
| `module.attr` / `mod.attr` | the receiver resolves to the target module | NAMED `attribute_receiver_unknown` (**blocking**) |
| `from M import name` | `M` IS the target module | NAMED `other_symbol_with_same_name` (advisory) |
| `import a.b.name` | `a.b` IS the target module | NAMED `other_symbol_with_same_name` (advisory) |
| keyword argument | the file is in the target's scope | NAMED `other_symbol_with_same_name` (advisory) |
| `global`/`nonlocal`, `except as`, parameter | the file is the defining module | NAMED `other_symbol_with_same_name` (advisory) |

A bare reference in a file that never imported the target stays **blocking**,
because a re-export or a namespace injection could make it the same symbol. A
same-named symbol in a *different* module is provably a different symbol, so it
is named but advisory - blocking on it would make every common name
unrenameable. `from <target> import *` is NAMED and blocking, because a star
import's bindings cannot be determined.

All of this is pinned by `TestPrecisionRegressions` (8 tests), including the
same-named-definition, qualified-attribute, relative-import and aliased-import
cases.

### 5. The completeness receipt - files considered, sites changed, and what it COULD NOT resolve

`CodemodReceipt` carries `files_considered` (the honest denominator: every file
the scan looked at, not only the ones that changed), `files_changed`,
`sites_found` / `sites_planned` / `sites_changed` (the last one measured by
`apply_plan`, never asserted), and `unresolved: tuple[UnresolvedSite, ...]` -
every site that could not be resolved, each NAMED with a path, a line, a kind
and a human reason. `CodemodPlan.with_applied(result)` folds the measured apply
counts in, so a rendered receipt states what happened rather than what was
proposed.

**`_completeness_pass`** is what makes a miss visible: after the role-aware AST
scan, every `tokenize` NAME / COMMENT / STRING token is checked against the
positions the scan already accounted for. An unaccounted NAME token is
blocking; a comment or a docstring mention is advisory. A string literal whose
value **is** the symbol's name (`globals()["compute_total"]`, a registry key,
an `importlib` argument) is `dynamic_symbol_lookup` and **blocking** - that is
the dynamic call site the brief is about. An f-string interpolating the name is
caught the same way.

`apply_plan` **refuses** a plan carrying blocking unresolved sites unless the
caller passes `allow_incomplete=True`, and the refusal names the first one.
`render_receipt` never omits the unresolved section: an empty list renders as an
explicit `(no unresolved sites)`, so "no unresolved sites" stays
distinguishable from "the list was dropped".

### 6. The apply goes through the ordinary edit path, and is all-or-nothing

`apply_plan(plan, backend, *, allow_incomplete=False)` dispatches every single
replacement as `backend.execute("edit", {...})` carrying the `expected_revision`
the plan read. Nothing in this module writes a file. That means a codemod
inherits, unchanged:

- the **stale-read guard** (`Workspace.apply_exact_edit`'s precondition);
- the **unique-match guard** (`require_unique`);
- the **protected-path / pre-existing-change policy**;
- the **approval policy** (`run_codemod(..., approve=None)` - the default -
  leaves the backend refusing an unapproved `edit`, so a standalone codemod
  cannot become the one caller that mutates a repository without the ordinary
  approval decision);
- the **undo journal** (`backend.execute("undo", ...)`).

The revision is advanced from each applied edit's own `post_hash`, so a second
edit to the same file is a fresh, correctly bound precondition rather than a
fabricated one. And the apply is **all-or-nothing**: if any single edit is
refused, every already-applied operation is undone in reverse through the same
backend's `undo` tool, and a rollback failure is reported rather than swallowed
(`rollback_failures`). A partially applied rename is exactly the state this
module exists to prevent.

A consequence worth knowing: **once the digest matches, every planned span is
still present.** The only two failure modes are a stale digest (refused) and a
policy refusal (rolled back). A rename cannot be half-right
(`test_a_matching_digest_makes_a_span_refusal_impossible`).

### 7. `update_signature` refuses to produce broken code

- The **declaration header** is rebuilt from the source between the
  `def`/`async def` line and the first body statement, sliced out of the
  ORIGINAL text so it carries the file's own line terminators, and the
  multi-line shape (and the closing-paren indent) is preserved.
- **`renamed` parameters**: the declaration, the function body's own reads, and
  the call sites' keyword arguments. The BODY matters - a parameter rename that
  missed the body is a guaranteed `NameError` the moment the change lands.
- **`removed` parameters**: the argument at that positional index is dropped from
  resolvable call sites (positionally mapped against the resolved callee's
  declared parameters, and by name for keyword arguments). A call using
  `*args`/`**kwargs` is NAMED `splat_arguments` and **blocking** - a wrong
  argument removal is a silent behaviour change. A call spanning several source
  lines is NAMED too, because rewriting it would reformat code this module does
  not own.
- **A removed parameter the body still reads is NAMED and BLOCKING.** There is
  no correct mechanical rewrite: whether the argument should be dropped,
  defaulted, or replaced is a semantic decision.
- **A name the body shadows anywhere** is named, not rewritten, for both cases -
  one shadowing rebind means the remaining references cannot be resolved
  without scope analysis this module does not perform.
- **`added` parameters are APPENDED** after the last declared parameter; a
  positional insertion point is the caller's decision. A bare-word default is
  rendered as a string (`currency:str=usd` -> `currency: str = "usd"`); anything
  else is raw source.
- **The rebuilt declaration is `compile()`d before any edit is planned.** A
  codemod must never leave a file that does not parse, and the common way to get
  there is appending a parameter with no default after one that has a default.
  That is a refusal naming the reason and the fix, not a broken file.
- The declaration edit is applied **LAST**, after the call-site edits in the
  same file, because rebuilding a multi-line parameter list changes the file's
  line count.

### 8. Unsupported languages refuse, before anything is collected

`RELIABLE_LANGUAGES == ("python",)`. `language_support(path)` returns `""` for
Python and a **reason naming the language and the missing extraction** for
everything else - JavaScript/TypeScript are refused by name, because the
extraction available there (`runtime.symbols`) is a conservative top-level
declaration scan that cannot enumerate call sites, imports, or string
references. The refusal happens **before any site is collected**, so an
unsupported-language codemod cannot partially apply. An unrecognised extension
is refused too, with its own reason. Pinned against a real `.js` and a real
`.ts` fixture whose bytes are asserted unchanged afterwards.

### 9. The catalog - two entries, no private list

`harness/tools.py` gains exactly two `_TYPED_TOOL_SPECS` entries, both
`side_effect_class="workspace_write"` with `approval=True`:

- `rename_symbol`: required `symbol`, `new_name`; optional `path`, `apply`,
  `allow_incomplete`.
- `update_signature`: required `symbol`; optional `path`, `added`, `removed`,
  `renamed`, `retyped`, `apply`, `allow_incomplete`.

No existing entry was modified. The kernel derives from this list, so
`catalog_parity_report` / `catalog_parity()` cover them automatically
(`TestCatalogParity` asserts the report is `identical` and names both). **The
catalog is now 45 tools.** Two entries rather than one `codemod` tool with a
discriminator, because a rename and a signature change are two intents and a
model picks the wrong one less often when it can say which.

`harness/agent_kernel/strategy.py` gains one additive nested handler `codemod`
plus `register("rename_symbol", codemod)` and
`register("update_signature", codemod)`. It always returns the rendered
receipt, refuses a refused plan with `error_kind="validation_error"`, returns
the plan alone when `apply=False`, and is honest (`error_kind="no_runtime"`,
"Nothing was changed") when no execution backend is bound - because a codemod
that wrote files itself would bypass the digest precondition, the unique-match
guard, the protected-path policy and the undo journal.

**The two tools are in no role profile.** `runtime/roles.py` was not edited, so
they are default-deny until a role profile lists them. That is the correct
default for a multi-file mutation, and exposing them is an owner decision (see
the requests below).

### 10. Config discipline - key presence, nothing in DEFAULTS

Every knob is read from `Task.config` by `config_from` and has a bounded
internal default in `CodemodConfig`. **None of them is in
`harness/config.py::DEFAULTS`**, and
`test_no_codemod_key_is_in_the_harness_defaults` fails if one is published: a
default is merged into every task and every eval arm, so it would silently
switch every run. `config.py` was not this round's file.

| key | default | what it bounds |
|---|---|---|
| `codemod_max_index_files` | 300 | **source files before the structural index is touched** |
| `codemod_scan_max_files` | 2000 | candidate files a scan may examine |
| `codemod_max_sites` | 2000 | planned sites |
| `codemod_edit_context_lines` | 2 | how far a non-unique span may be widened |
| `codemod_include_paths` | none | narrows the walk to repo-relative prefixes |
| `codemod_follow_string_references` | False | named for completeness; strings are advisory either way |

`codemod_max_index_files` exists because the handler must not do unbounded work
before it can answer. Building the tree-sitter index for THIS repository
(435 `.py` files) was **measured at over 600 seconds** - it hung the first test
run. The bound is checked from a cheap directory walk *before* `_load_index` is
called, so on an oversized repository the handler refuses in **0.9s** with a
reason naming the count and the key. A limit enforced after the expensive step
is no limit at all.

`config_from` **raises** `TypeError` on a non-integer bound rather than
coercing it, and an unusable bound makes the plan REFUSE (a `TypeError` never
escapes into a run). An out-of-range bound is clamped and the clamp is recorded
in `notes` - never silently applied.

### 11. Two real defects this module's own tests found (both fixed, both pinned)

1. **The definition keyword was renamed.** A `FunctionDef`/`ClassDef` node's
   `col_offset` points at `def`/`async def`/`class`, not at the name, so the
   first version produced `compute_amountotal(items, tax=0.0):`. The name
   token is now located on the declaration line from the node's own column
   onwards, and a name that cannot be located is named rather than guessed at.
   Pinned by `test_the_definition_keyword_is_never_renamed` and the async and
   decorated cases.
2. **A CRLF file could never be edited.** Edit blocks and the declaration
   header were re-joined with `"\n"`, which does not occur in a file written on
   Windows, so every span looked ambiguous and every plan refused ordinary
   work. Blocks are now sliced out of the ORIGINAL text and the rebuilt
   declaration uses the file's own terminator. This is why the required
   "changes exactly those" test was red on Windows and green on the fixture I
   first tried by hand.

Also found while wiring: the kernel handler treated `apply_plan`'s return as an
outcome wrapper and read `.applied` off a `CodemodApplyResult`, so every
dispatch returned `TOOL ERROR [AttributeError]: 'tuple' object has no
attribute 'ok'`. `apply_plan` returns the apply result directly; the handler
now does too, and reports the undo handles in the result body because the
kernel's `ToolResult` carries no operation id.

### 12. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_ceiling_r2_07_codemod.py` -> 80 passed.** Host-only: no
  Docker, no model, no network. The edit path under test is the REAL
  `execution.workspace.SafeToolBackend` over a real workspace, so the digest
  precondition, the unique-match guard, the approval policy and the undo
  journal exercised here are the production ones, not a double. The five
  required proofs are present and named:
  `test_a_rename_touches_the_definition_n_call_sites_and_the_import_and_nothing_else`,
  `test_a_dynamic_reference_is_named_in_the_receipt_not_skipped_silently`,
  `test_a_rename_in_javascript_refuses_with_a_reason_and_changes_nothing`,
  `test_a_concurrent_write_after_planning_is_refused_by_the_stale_read_guard`
  and `test_the_catalog_parity_report_includes_the_new_codemod_tools`.
- `tests/test_tool_protocol.py tests/test_workspace_security.py
  tests/test_ceiling05_knowledge.py tests/test_code_graph.py
  tests/test_multilang_graph.py` -> **159 passed, 1 skipped** (the skip is a
  Windows symlink-privilege case). The one skip is a blocked platform case,
  not a pass.
- `tests/test_tool_protocol.py` alone -> **29 passed** (the catalog-parity and
  handler-coverage suites that the two new entries feed).
- `tests/test_agent_kernel.py tests/test_agent_loop.py
  tests/test_config_trace_state.py tests/test_orchestration.py` -> **138 passed,
  4 failed**, all four NOT from this round and none counted as passes:
  - `test_agent_kernel.py::test_safe_backend_overwrites_and_rolls_back_invalid_syntax`
    and `::test_hard_kill_and_resume_retain_pre_kill_turns_and_evidence` -
    both on the `write` path. **Proven, not assumed:** removing
    `register("rename_symbol", codemod)` and
    `register("update_signature", codemod)` entirely and re-running them
    reproduces both failures identically. Nothing in this round's diff touches
    `write`, `valid_python`, or `WorkspaceJournal`.
  - `test_orchestration.py::TestOrchestratorSubprocess::test_real_docker_verifier_child_runs_in_isolated_worktree`
    and `::test_child_wall_timeout_retries_and_resumes` - the documented
    host-load hazard already recorded in `runtime/AGENTS.md` (a 3.0s child
    wall-clock budget against a ~30s `import litellm` on this machine).
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0. No prompt in this
  round, so the prompt-regression matrix is unchanged by construction as well
  as by measurement.
- `ruff check` and `ruff format --check` clean on both NEW files;
  `python -m compileall -q` clean on all three touched modules; scoped
  `git diff --check` clean.
- **No Docker lane and no live-provider lane were run.** They are NOT selected
  and are NOT reported as passes. No credential was inspected or retained.

An earlier run of the code-graph selection showed 32 failures with
`NameError: name 'iter_source_entries'`. Those were **another terminal's
in-flight edit to `memory/code_graph.py`** (a half-landed extraction refactor):
re-run minutes later, both `tests/test_code_graph.py` (16 passed, 1 skipped)
and `tests/test_multilang_graph.py` (34 passed) were green, and the
independent probe `CodeGraphBuilder(tempdir).build()` succeeded. Not this
round, and no repair was made to another module's file.

### 13. Not implemented, stated plainly

- **No JS/TS codemod.** `memory.code_graph` has a real tree-sitter JS/TS
  indexer, so a future round could resolve JS/TS sites as precisely as Python
  ones. The blocker is that `runtime.symbols`' conservative scan is the only
  documented *language* surface, and this round refuses rather than guessing.
  A JS/TS implementation belongs beside a polyglot round, not here.
- **No `delete`/`extract`/`move` codemod**, and no rename of a *method* by its
  qualified path (a bare method name is ambiguous across classes, and
  ambiguity is a refusal here).
- **No shadowing resolution.** A name the body rebinds anywhere is NAMED, not
  rewritten. Real scope analysis would let the codemod handle it; this round
  names it and blocks, which is the honest direction.
- **Added parameters are appended only.** There is no "insert after this
  parameter"; a positional insertion point is a design decision the caller owns.
- **A multi-line call is never rewritten.** It is named instead, because
  rewriting it would reformat code this module does not own.
- **`codemod_follow_string_references` is read but changes no behaviour yet.**
  String and docstring mentions are advisory; the flag is the seam for a future
  "rewrite prose too" mode, and it is documented as inert rather than
  pretending otherwise.
- **No rollback verification.** A rollback failure is REPORTED
  (`rollback_failures`) but nothing re-reads the files to confirm the residue
  is gone.
- **Not wired into `harness/core.py` / `harness/agent_loop.py`.** The two tools
  are in the ONE catalog, so the strict kernel sees them; the legacy bash-only
  fix loop does not, and adding them there is another owner's file.
- **Not on any role profile**, so no agent can currently call them.

### 14. Cross-terminal requests

1. **`harness/config.py` owner (R2-04):** the six `codemod_*` keys are
   intentionally absent from `DEFAULTS`; `test_no_codemod_key_is_in_the_harness_defaults`
   will FAIL if they are added. For discoverability you may add them at
   `None` **only if** that is behaviour-neutral - it is not, for
   `codemod_follow_string_references` (currently inert, so `None` is safe) but
   `codemod_max_index_files=None` would be coerced by `config_from`'s
   `_int` and therefore break. The recommended change is none at all; the
   module's own `CodemodConfig` docstring documents each key.
2. **`runtime/roles.py` owner:** the two tools are default-deny because no
   profile lists them. If an implementer or reviewer role should get them,
   add `rename_symbol` and `update_signature` to that profile's
   `visible_tools` and to its policy rules - they are `workspace_write` and
   approval-gated, so a deny-by-default profile that does not mention them
   keeps refusing, which is the right behaviour until someone decides
   otherwise. Nothing in `harness/` needs to change for that.
3. **`harness/agent_loop.py` owner (R2-04):** the general agent's legacy loop
   has its own tool vocabulary and does not read the typed catalog. Exposing
   the two codemods there means routing their apply through
   `execution.workspace.SafeToolBackend` exactly as the kernel handler does
   (see `harness/agent_kernel/strategy.py::codemod`), or the digest and
   unique-match guards are bypassed. Do **not** call `harness.codemod`'s apply
   helpers with a hand-rolled writer.
4. **`INTERFACES.md` owner:** the catalog is now **45 tools** with two
   `workspace_write` + approval-gated entries, `harness/codemod.py` is a new
   harness module with a `SCHEMA_VERSION = 1` receipt, and no Boundary 0-5
   signature changed. `INTERFACES.md` was not this round's file, so the
   Change Log entry is filed here rather than written - paste it verbatim
   under "Change Log" dated 2026-09-26 (R2-07) if you own that file:

   > - 2026-09-26 (R2-07 - the AST codemod layer): **NEW
   >   `harness/codemod.py`; `harness/tools.py` gains two additive
   >   `_TYPED_TOOL_SPECS` entries (`rename_symbol`, `update_signature`, both
   >   `workspace_write` + `approval=True`); `harness/agent_kernel/strategy.py`
   >   gains ONE additive handler plus two registrations. No Boundary 0-5
   >   signature, event kind, serialized field, or completion status changed,
   >   and no existing catalog entry was modified. The catalog is 45 tools.
   >   `harness/config.py`, `harness/core.py`, `harness/agent_loop.py` and every
   >   verifier were NOT edited.** (1) **A codemod is a PLAN.**
   >   `rename_symbol` / `update_signature` return a `CodemodPlan` of exact
   >   per-line `old_string`/`new_string` pairs and change nothing;
   >   `apply_plan` dispatches each pair through the ordinary
   >   `SafeToolBackend.execute("edit", ...)` with the `expected_revision` the
   >   plan read, so the stale-read guard, the unique-match guard, the
   >   protected-path policy, the approval policy and the undo journal all
   >   apply unchanged. The apply is ALL-OR-NOTHING: one refusal undoes every
   >   applied operation in reverse through the same backend's `undo`.
   >   (2) **The receipt is the product.** `CodemodReceipt` carries
   >   `files_considered` (the honest denominator), `files_changed`,
   >   `sites_found`/`sites_planned`/`sites_changed` (measured by the apply),
   >   and `unresolved` - every site the codemod could not resolve, each NAMED
   >   with a path, line, kind and reason. Blocking kinds refuse the apply
   >   unless `allow_incomplete=True`; advisory kinds (a comment, a docstring, a
   >   provably different same-named symbol) are named and do not block. A
   >   string literal whose value IS the symbol's name is
   >   `dynamic_symbol_lookup` and BLOCKING, which is the dynamic call site the
   >   round exists to catch. (3) **Python is resolved precisely, not by
   >   name.** Which symbol/module comes from `memory.code_graph`; which
   >   line/column from stdlib `ast` (the parser `runtime.symbols` and
   >   `harness.lint` already use) plus stdlib `tokenize`; which languages are
   >   reliable from `runtime.symbols`' suffix sets. No second parser, no
   >   duplicate index. A same-named symbol in a different module is provably a
   >   different symbol and is never rewritten - a name-based
   >   over-approximation did that, and it was a real defect this round found
   >   and fixed. (4) **Unsupported languages refuse before any site is
   >   collected.** `RELIABLE_LANGUAGES == ("python",)`; JS/TS are refused by
   >   name with the reason, and the file is byte-identical afterwards.
   >   (5) **Config.** Six `codemod_*` keys read from `Task.config` with
   >   bounded internal defaults, and **none is in `DEFAULTS`** (a default is
   >   merged into every task and every eval arm). `codemod_max_index_files`
   >   (300) is checked from a cheap walk BEFORE the structural index is
   >   touched - indexing this repository measured over 600s, so a bound
   >   enforced after the expensive step would be no bound at all; the
   >   oversized case refuses in 0.9s. A non-integer bound raises rather than
   >   coercing; an out-of-range bound is clamped WITH a note.
   >   Verified: `tests/test_ceiling_r2_07_codemod.py` -> **80 passed**
   >   (host-only, real `SafeToolBackend`); `tests/test_tool_protocol.py
   >   tests/test_workspace_security.py tests/test_ceiling05_knowledge.py
   >   tests/test_code_graph.py tests/test_multilang_graph.py` -> **159 passed,
   >   1 skipped**; `python -m evals.run --check` -> **14/14 CLEAN**; ruff check
   >   and format clean on both new files. **4 pre-existing failures in
   >   `test_agent_kernel.py`/`test_orchestration.py` are NOT from this round
   >   and are not counted as passes** - the two `write`-path ones were proven
   >   by removing this round's two `register(...)` lines and reproducing them
   >   identically. **No Docker lane and no live-provider lane were run.**
   >   Not implemented: no JS/TS codemod, no shadowing resolution, no
   >   multi-line call rewriting, no positional parameter insertion, and no role
   >   profile exposes the two tools yet.

---

## R2-12 - the harness side of the ecosystem registry (2026-09-27)

**R2-12's files are `execution/ecosystems.py` (new), `execution/verify.py`,
`harness/editor.py` (ONE additive keyword-only parameter) and
`harness/config.py` (two `None` entries). This section records what the harness
owner needs to know, and what was deliberately NOT done here.**

### 1. The hole that existed, in one line

`DEFAULTS["protected_paths"]` is `["tests/*", "test_*.py", "*_test.py"]` and it
matches **no Java or Go test file at all**:

```python
is_protected("src/test/java/com/x/CalcTest.java", ["tests/*", "test_*.py", "*_test.py"])
# -> False
is_protected("internal/mathutil/add_test.go",     ["tests/*", "test_*.py", "*_test.py"])
# -> False
```

So a `run_task` in a Maven or Go repository had **no protected test surface**.
Both facts are pinned in `tests/test_ceiling_r2_12_polyglot.py::TestProtected
PathsAreLanguageCorrect` so the fix cannot silently regress.

### 2. What was changed in the harness, and how little

**`harness/editor.py` - exactly ONE change.**
`extended_protected_patterns(protected_patterns=None, *, language=None)` gained
`ecosystem=None`. It accepts an `execution.ecosystems.Ecosystem` or a registered
NAME and adds that language's test globs. Additive, keyword-only, `None`
default, registry import guarded and lazy - so every existing
three-positional-argument call site is byte-identical and the editor stays
importable without `execution`.

**`is_protected`'s two-positional-argument signature is UNCHANGED**, and nothing
else in the 1700-line editor was touched. `extended_protected_patterns` still has
no production caller, so this change is currently inert on the run path - the
resolution points are filed requests (below), not done.

**`harness/config.py` - two `None` entries**, `ecosystem` and
`ecosystem_protected_paths`, each with a comment saying why `None` and not a
value. `DEFAULTS["protected_paths"]` is untouched, and that is deliberate:
changing it to be language-agnostic would either weaken the Python guarantee
(drop the globs) or over-block every other language (ship every ecosystem's
globs into every repository). The per-language answer is applied AT RESOLUTION
TIME instead, which is what the filed requests do.

`test_no_ecosystem_default_switches_every_run` iterates `DEFAULTS` and FAILS if
either key is ever given a real value, and
`test_protected_paths_default_is_untouched_by_this_round` pins the Python list.

### 3. What the harness owner should NOT do

- **Do not add the ecosystem globs to `DEFAULTS["protected_paths"]`.** It is
  merged into every task and every eval arm; a Go glob there would start
  protecting `*_test.go` in Python repositories and every other one, and a
  Java glob set would do the same. Resolve per run.
- **Do not relax `is_protected`.** It remains the single enforcement point, and
  it is unchanged.
- **Do not make the zero-test policy configurable off.** `ZERO_TEST_POLICIES` is
  a closed set of two, both fail-closed, and `register_ecosystem` refuses
  anything else. There is deliberately no key that disables it - the same
  discipline R2-03 used for `test_config_change` ("a refusal cannot be
  configured away, only declared").

### 4. Cross-terminal requests filed by this round (harness owner's files)

1. **`harness/core.py` - TWO call sites, and they are the whole wiring.**
   `core.py:669` and `core.py:2540` both do
   `protected = [str(p) for p in (cfg.get("protected_paths") or [])]`. Each
   should become
   `list(effective_protected_paths(cfg.get("protected_paths"), eco=cfg.get("ecosystem") or None))`.
   The `_authorized_test_target` strip that follows BOTH must then drop the
   ECOSYSTEM's globs rather than the three Python literals, or a declared
   "fix the tests" task would be refused on `*_test.go` while still allowed on
   `tests/*`. `eco=` takes a name string, so a caller can pin the ecosystem
   without importing the registry.
2. **`harness/agent_loop.py` - the same at the interactive tool layer.**
   `agent_loop.py:1458` and `:2190` feed the EDIT/WRITE refusals (`:2335`,
   `:2369`) and the BASH change audit (`:1794`).
   `extended_protected_patterns(protected, ecosystem=cfg.get("ecosystem") or None)`
   is the one-line additive form.
3. **`harness/agent_kernel/strategy.py` / `runtime/roles.py` - no change needed
   today.** The typed catalog's protected-path policy reads
   `protected_paths` from its own context; when it wants the ecosystem globs it
   should call `effective_protected_paths` at the same point rather than
   growing a second list. Nothing in `harness/agent_kernel/` was edited by this
   round.
4. **`cli/main.py` - the `--protected` flag.** It writes
   `config["protected_paths"]` at `:114` and `:1102`. If the CLI wants
   language-correct early feedback it can call
   `extended_protected_patterns(protected, ecosystem=ecosystem_name)` there.
   R2-03 filed the same request for the test-config surfaces; this is the same
   line.

### 5. Verification actually run for the harness-side edits

- `tests/test_ceiling_r2_12_polyglot.py` -> **79 passed, 1 skipped** (the skip
  is the image-gated Go-toolchain lane; BLOCKED coverage, not a pass).
- `tests/test_editor_prompts.py tests/test_config_trace_state.py
  tests/test_stubs_and_deps.py tests/test_adversarial.py
  tests/test_ceiling_r2_06_editing.py tests/test_ceiling_r2_07_codemod.py` ->
  **207 passed, 1 skipped** (a Windows symlink-privilege case).
- `tests/test_modes.py tests/test_coordination_e2e.py` -> **104 passed**;
  `tests/test_e2e_run_task.py tests/test_git_output_rationale.py` -> **59
  passed**. `python -m evals.run --check` -> **14/14 CLEAN**.
  `python -m evals.run --suite daily-driver --no-docker --json` -> **50/52 case
  arms ok**, `zero_false_verified_successes=true`, **28/28 feature-evidence
  arms pass**.
- **Two red results, both proven not from this round and neither weakened** -
  the full A/B for each is in `execution/AGENTS.md` section 8: a host wall-clock
  pin in `test_ceiling_r2_02_flake.py` that passes 3/3 standalone, and
  `dd_20_live_tui_status_diff` which is still red with the R2-12 path provably
  inert in every process.
- **A disclosed edit to one file outside this round's set:** three assertions in
  `tests/test_ceiling08_verification.py::TestIncrementalSelection` pinned the
  literal dispatched command. They were made STRONGER (asserting "the full
  suite ran and no test path leaked") rather than relaxed, via a documented
  `_runs_the_full_suite_only` helper.
- `ruff check` clean on both harness files. **No formatter was run on
  `harness/editor.py` or `harness/config.py`** (shared dirty files with parallel
  in-flight edits, per the shared-file protocol).
- **No live-provider lane and no Docker-lane eval matrix were run** and neither
  is claimed.

## AGT-10 - queued messages at the tool-batch boundary (2026-09-28)

**Owns: `harness/steering.py` (one new section), `harness/tools.py` (three
additive public names), `harness/agent_loop.py` (the `_run_agent_legacy` turn
loop), and NEW `tests/test_agt_10_batch_boundary.py`. Did NOT touch
`harness/core.py`, `harness/agent_kernel/**`, `execution/**`, `harness/config.py`,
any verifier, or the mint condition. AGT-02 handed `harness/agent_loop.py` to
this round in its own section 8.4; nothing AGT-02 built was changed.**

The gap: steering existed, but it was observable only BETWEEN model calls. A
correction typed while a `sleep 300` was still dispatched could not be seen
until the command returned, and a correction typed during the model call that
produced `DONE` was swallowed by the run that reported a clean finish. The
transport (`logs/{task_id}/steering.jsonl`) and the consume authority
(`SteeringBuffer.take`) are unchanged. This round adds a second CONSUMER and
the two audit facts a queue needs.

### 1. Three new names, and what each one is for

| name | file | job |
|---|---|---|
| `QueuedSteering` | `harness/steering.py` | the batch-boundary VIEW of one task's inbox |
| `QueuedSteeringWatcher` | `harness/steering.py` | records arrivals WHILE a batch runs; never interrupts |
| `SteeringDelivery` | `harness/steering.py` | what one seam consumed, already partitioned |
| `partition_intents(events)` | `harness/steering.py` | ONE definition of `abort > replan > guide` |
| `run_tool_batch` / `ToolBatch` / `ToolBatchStep` | `harness/tools.py` | the seam primitive: N calls, N safe seams |

`SteeringBuffer` gained exactly one method, `append_record(obj)`: a public
"append one non-state journal row". `queue` and `deliver` are new `op` values
in the same file, appended through the same single-write append as `inject`
and `consume`. **`_scan` still folds only `inject` and `consume`**, which is
what makes the new rows safe to add to a journal an older reader already has -
pinned by `test_the_audit_rows_do_not_change_replayed_state`, which also
proves a message recorded as queued but never consumed is still PENDING after
a replay.

### 2. The two safety rules are STRUCTURAL, not conventions

- **A mutating call is never split to make room for a message.**
  `run_tool_batch` calls `dispatch(call)` exactly once per call and calls
  `seam` only between calls. Pinned by
  `test_a_mid_batch_delivery_never_splits_a_mutating_call`, whose dispatcher
  marks itself in-flight on entry and clears it on exit, so a mid-call seam
  would observe `True`.
- **A seam that raises stops the batch.** Running the next mutation after the
  correction failed to land is the failure this primitive exists to prevent, so
  an exception is treated as a refusal, not as "carry on"
  (`test_a_raising_seam_stops_the_batch_rather_than_running_on`).
- **`QueuedSteeringWatcher` has no hook, no session and no kill path.** It can
  only observe. Interrupting an in-flight command remains `HardAbortWatcher`'s
  one job, for the `abort` intent only, exactly as VEX-CEILING-07 G14 built it.

### 3. The loop seam, and why the legacy path's payoff is small (stated)

The legacy loop's protocol is ONE tool call per reply, so its "batch" is
size 1 and the seam is the end of the turn. The mechanism is still real and
shared: `run_tool_batch` gives a dispatcher that really does run N calls per
turn N seams from the same code, and the seam is where the three concrete
wins live:

- **A correction is in the messages of the very next model call**, and its
  receipt names the turn whose tool ran (`at="agent-batch-1"`, `turn: 1`) -
  not the turn that made the model call. Pinned by
  `test_a_correction_typed_during_a_running_tool_reaches_the_model_in_the_`
  `same_turn`, which also asserts the turn-1 call did NOT have the text, so
  the test discriminates.
- **A replan at a seam does not burn a turn.** At the TURN boundary a replan
  `continue`s past the model call, so the turn is spent on nothing; at a seam
  the tool result is already in the conversation, so the guidance rides the
  next model call (`test_a_replan_at_the_seam_does_not_burn_a_turn`).
- **A message is delivered after the LAST call of the final tool batch**, which
  is what the final gate below depends on.

### 4. The final gate is a STRENGTHENING, and it was this round's obligation

`DONE` is refused when the queue is non-empty: the pending text is delivered,
`steering_final_gate_deferred` is emitted, and the loop continues. This is
only a strengthening - with `steering_enabled: False` the queue is empty and
the branch is one `if` on an empty value
(`test_steering_off_is_one_code_path_and_never_defers_a_result`).

It exists because this round ADDED a consume point, and a consume point is
exactly where a typed instruction can be swallowed by the turn that ends the
run. The pre-existing top-of-turn checkpoint cannot see a message that arrived
during the model call that produced `DONE`, because the checkpoint ran before
that call. `test_a_correction_that_contradicts_done_defers_the_result` proves
the deferral and that the run still finishes on the model's SECOND, corrected
answer.

**The verifier gate is untouched and the DONE branch is asserted by reading
the source**: `test_the_verifier_gate_is_not_weakened_by_this_round` splits
`harness/agent_loop.py` at the last `if tool == "done":`, and asserts the
statements AFTER `answer = str(` contain no `steering` / `QueuedSteering` /
`queued` / `deliver`, no `completed_unverified`, and still key the mint on
`verification.get("target_passed")` and `not verification.get("flaky")`.

### 5. Visible queueing, visible consumption, and the "never delivered" report

`QueuedSteering.receipt()` is the audit, and `undelivered` is the
load-bearing key: the seqs this queue SAW and never handed to a consumer. A
non-empty list means a message the user typed is sitting in the journal, which
the run says out loud (`steering_queue` trace row + `AgentResult["steering_queue"]`)
rather than reporting a clean finish over it. The stranded case is reachable
and is pinned end to end: a turn that ends because AGT-02's reflection budget
is spent sets `_batch_stop`, the seam refuses to deliver, and
`test_a_message_that_is_queued_and_never_delivered_is_reported` asserts
`undelivered == [1]` AND that the event is still PENDING in the journal.

**Every consume in the loop now goes through the queue** - the pre-dispatch
abort and the in-flight abort were rerouted from `steer.take(...)` to
`queued.deliver(...)`. `SteeringBuffer.take` is still the only writer of a
`consume` row; the reroute only means a receipt that saw just the SEAM's
deliveries would under-report a real one. `test_an_abort_is_taken_once_and_`
`never_double_consumed` asserts exactly one `consume` for one injected abort.

`queued_without_watcher` is a deliberate distinction, not a wart: a message
typed while the model was THINKING had no watcher behind it, and reporting it
as an in-flight observation would be a lie in the other direction.

**A delivery never spends reflection budget.** A queued message is not a
failure, and a user talking to the agent must not be able to exhaust the
run's recovery budget (`test_a_delivery_never_spends_reflection_budget`).

### 6. Config

**No `harness/config.py` key, deliberately.** This round adds a mechanism, not
a bound: the unconsumed cap is already `max_pending_steering` (a shared
`SteeringBuffer` refusal, so this round cannot refuse a message an earlier
round accepted), and the poll cadence is the same 50 ms the abort watcher
already uses. `DEFAULT_MAX_QUEUE_ROWS` (64) is a module-level constant in
`harness/steering.py` and bounds the AUDIT only - an arrival whose `queue` row
could not be journalled is reported in `queue_rows_unwritten` rather than
claimed as acknowledged. Publishing either in `DEFAULTS` would silently switch
every task and every eval arm.

### 7. Trace events (all additive)

`steering_batch_boundary` {turn, at, tool, action, seqs, intents, texts,
max_wait_s} - the seam, on every non-empty delivery.
`steering_queue` {queued, queued_seqs, delivered, delivered_seqs, deliveries,
undelivered, queue_rows_unwritten, queued_without_watcher, task_id, enabled} -
once, before `task_end`, only when anything was queued or delivered.
`steering_queue_watcher_error` {turn, errors} - a journal the watcher could not
read. `steering_final_gate_deferred` {turn, at, texts, reason} - a `DONE` that
was refused because the queue was non-empty.

The loop-side receipts stay the historical kinds with the historical shapes
(`steering` / `steering_replan` / `steering_abort`); the only addition is
`waited_s` on `steering`. The turn-boundary `where` slugs
(`abort-agent-turn` / `replan-agent-turn` / `agent-turn-N`) are unchanged.

**`cli/runview.py` does not classify any of the four new kinds.** The same gap
AGT-02 filed for `reflection` applies here, and the same one-line fix
(`EVENT_VOCABULARY` / `INFORMATIONAL_EVENTS`) covers all five. `cli/runview.py`
is not this round's file and was not edited.

### 8. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_agt_10_batch_boundary.py` -> 23 passed.** Host-only: no
  Docker, no provider, no network. All five required proofs are present and
  named (see the module docstring for the mapping). The "a tool is running"
  condition is a real background thread with a real `threading.Event`
  handshake, not a simulated clock.
- `tests/test_agent_loop.py` -> **62 passed**;
  `test_agt_02_reflection.py` + `test_recovery_steering.py` -> **89 passed**.
- `test_agt_10_batch_boundary` + `test_agt_04_search_budgets` +
  `test_tool_protocol` + `test_config_trace_state` +
  `test_ceiling_r2_04_daily_default` -> **127 passed, 1 skipped** (the skip is
  a Windows symlink-privilege case).
- `test_streaming_ui.py` + `test_modes.py` + `test_cli_runview.py` ->
  **279 passed**.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0.
- `python -m evals.run --suite daily-driver --no-docker --json` ->
  **49/52 arms ok**, `zero_false_verified_successes=true`,
  `zero_unauthorized_mutations=true`, `zero_lost_edits=true`,
  `valid_comparison_count=24`, cost $0.0246. Verdict `NOT_READY` / `ERROR`,
  reported honestly. Report:
  `logs/evals/20260928-224745-d2b6b38e4c32441186350e8c7dfec3d8/daily_driver_report.json`.
- `ruff check` clean on all three owned files plus the new test;
  `compileall` clean; scoped `git diff --check` clean.

### 9. Red results, attributed and NOT weakened

1. **5 failures in `tests/test_steering.py::TestSteeringE2E` and 2 arms of
   `dd_05_skill_model_context` share ONE root cause, and it is NOT this
   round.** All seven end with
   `planner failed: ScriptedModel.__call__() got an unexpected keyword argument 'effort'`.
   `harness/core.py`, `harness/model_client.py` and `runtime/model_router.py`
   are all uncommitted-modified in this tree (another terminal), and
   `ModelClient.call` now takes `effort=`, which it forwards to the boundary -
   while `tests/fake_model.py::ScriptedModel.__call__` has the strict
   signature `(messages, difficulty_hint, provider, model, api_key)`.
   **These paths never reach this round's code**: they are
   `harness.core.run_task`'s planner, and this round edited no file on it.
   The fix belongs to whoever added `effort=` (or to
   `tests/fake_model.py`, which every other terminal's suites also read). Not
   counted as a pass.
2. `dd_20_live_tui_status_diff/baseline` is the arm R2-12 section 5 and AGT-04
   section 8 both already recorded as red / host-load-flaky. Untouched here.

### 10. Not implemented, stated plainly

- **`harness/agent_kernel/strategy.py` has NO steering integration at all**
  (verified: zero occurrences of `steer`/`steering` in the file). The typed
  kernel's `_execute_calls` runs its batch through
  `_execute_by_concurrency_class`, whose sequential arm is a tight loop with
  no seam and whose concurrent arm submits everything to a
  `ThreadPoolExecutor` at once, so there is no per-call boundary to attach to
  without changing its dispatch. The primitive is ready
  (`harness.tools.run_tool_batch`); the wiring is a separate decision for the
  kernel's owner, and a concurrent fan-out has a genuinely different seam
  question (a delivery between two CONCURRENT calls has no "before" to attach
  to). **This round does not claim the daily path delivers queued messages.**
- **`harness/core.py`'s fix loop has no batch-boundary delivery.** It keeps its
  turn-boundary and step-boundary checkpoints unchanged; its per-step session
  executes one command per turn, so it has the same size-1 property.
- **A queue is capped only by the existing `max_pending_steering`.** A burst
  larger than the cap is refused honestly by the shared buffer (as it always
  was), not by anything added here.
- **`max_wait_s` is measured, not bounded.** There is no staleness alarm: a
  message that waits a long time because a long tool was running is reported
  with its real wait and nothing else. Deciding whether a sufficiently stale
  correction should be escalated is a product decision, not a transport one.
- **No CLI surface.** The REPL/TUI already queue and preserve order on their
  side (`tests/test_streaming_ui.py::TestSteeringWhileRunning`); this round
  makes the LOOP consume faster, and the new `steering_batch_boundary` /
  `steering_queue` kinds are not yet mapped in `cli/runview.py`.

### 11. Cross-terminal requests

1. **`harness/agent_kernel/strategy.py` (kernel owner) - the real one.**
   `_execute_by_concurrency_class` is where a daily-path batch-boundary would
   go. `harness.tools.run_tool_batch` is ready and needs no import from the
   kernel's internals. Two things to decide, not to guess: (a) the sequential
   arm can take a seam directly, but (b) the concurrent arm cannot, because a
   delivery between two already-submitted calls has no completion ordering to
   attach to. A defensible answer is "deliver only between concurrency
   CLASSES, after the parallel group drains" - which is honest, and strictly
   better than nothing, and does not require splitting a fan-out.
2. **`cli/runview.py` owner - five unmapped event kinds.** Add
   `steering_batch_boundary`, `steering_queue`,
   `steering_queue_watcher_error`, `steering_final_gate_deferred` and
   `reflection` to `EVENT_VOCABULAR` (or `INFORMATIONAL_EVENTS` for the
   receipt-style ones). Without it a live TUI run renders them as unmapped
   kinds (`cli/tui.py:2876`). The same one-line fix AGT-02 filed.
3. **`harness/model_client.py` / `runtime/model_router.py` / `core.py` owner -
   this is a real regression, not a request.** The `effort=` kwarg added to
   `ModelClient.call` is forwarded to the model boundary, and
   `tests/fake_model.py::ScriptedModel` does not accept it, so EVERY
   `harness.core.run_task` test and daily-driver case that scripts the planner
   dies with `planner failed: ... unexpected keyword argument 'effort'`
   (measured: 5 `test_steering.py` e2e tests + `dd_05` in both arms). Either
   the boundary should forward `effort` only to a callable whose signature
   accepts it (the same rule `BashSession` already applies to
   `cancellation_token`), or `ScriptedModel` should take `**kwargs`. **Do not
   change the test fixture's expectations to hide it.**
4. **`harness/config.py` owner - nothing is required.** This round
   intentionally published no `DEFAULTS` key (section 6). If you want
   `steering_batch_poll_interval_s` discoverable, a `None` entry is
   behaviour-neutral; a real value would silently switch every run.

## AGT-09 - staged undo with three restore granularities (2026-09-28)

**Owns:** `memory/checkpoints.py` (additive `StagedSnapshotStore` block),
`harness/editor.py` (additive `StageCapture` block + six one-line hooks),
`cli/fileview.py` (additive projection + the shared `undo_command`
dispatcher), `cli/tui.py` (the `/undo` surface + the new-prompt commit),
`cli/interactive.py` (the same, REPL idiom), `cli/commands.py` (one
`/undo` spec's summary and argument hint), and NEW
`tests/test_agt_09_staged_undo.py`. **No `INTERFACES.md` signature, event kind,
serialized field, exit code, or completion status changed, and no
`harness/config.py` `DEFAULTS` key was added.** `cli/tui.py` and
`cli/interactive.py` were shared dirty files; the edits are surgical and
additive, and the regions touched are listed below so a rebase can find them.

### 1. What landed

A second, deliberately different mechanism sits next to `CheckpointManager` in
`memory/checkpoints.py`. A checkpoint stack is a list you POP; it is not
scriptable, not idempotent, and it cannot express a RANGE. The staged store is:

- a **content-addressed private object store** at
  `<log_root>/_undo/<session>/objects/<aa>/<sha256>` - identical content is
  stored once, across turns and across paths;
- an **append-only journal** `<log_root>/_undo/<session>/journal.jsonl` that is
  the authority for which turns exist and in what order;
- a single **staging pointer** `staged.json`, the authority for the currently
  staged range.

`StagedSnapshotStore.capture(...)` NEVER raises. A failure is a
`status="failed"` record plus an `undo_snapshot_failed` trace event, because
losing a user's work to a bookkeeping failure is the one outcome this feature
must not produce.

### 2. The three granularities, and why the names are the ones they are

`RESTORE_SCOPES = ("files", "conversation", "both")` is the vocabulary the
product ALREADY has for the context-budget rewind
(`harness.agent_kernel.context.rewind_run`). It is reused rather than
reinvented, and pinned EQUAL by
`TestThreeGranularities::test_the_three_scopes_are_the_products_own_rewind_vocabulary`
because `memory` may not import `harness` (the dependency direction is
cli -> memory). The same technique the edit-refusal slugs already use.

**`files` is the DEFAULT**, because rewinding the code and KEEPING the
conversation is what a user wants almost every time. The scope genuinely gates
its axis - a `conversation` revert writes no file at all, and the receipt says
`restored: []`.

### 3. The rule that took three attempts to get right

**"The file differs from the pre-image" is NOT evidence of a concurrent user
edit.** A revert is SUPPOSED to overwrite the change its own turn made. The
first implementation refused on that difference and refused every ordinary
revert.

The rule that is actually correct: a step captures the pre-image of a path ONCE
per turn and the post-image after EVERY mutation, so **every content the run
produced is recorded**. A current hash that appears nowhere in the staged range
is the only real evidence that a third party wrote it, and that is the only
thing refused. `_accounted_hashes` implements it for files and
`_accounted_conversation` for the conversation, for the same reason - a
conversation grows as the run talks and a `conversation` revert is supposed to
rewind that growth.

This is why the `harness/editor.py` hooks capture AFTER every mutation rather
than once per turn. An optimisation that only captured per turn would make the
second edit of a turn look like a concurrent user edit.

### 4. Two honesty defects this round's own tests found

- **`all([]) is True`.** `verified` was computed as
  `all(item["verified"] for item in restored)`, so a receipt that restored
  NOTHING and refused everything claimed it was verified. `verified` now
  requires `checked_paths > 0`, and the renderer distinguishes "NO - see the
  refusals" from "NO - nothing was checked".
- **`force=True` silently skipped the record.** The force path bypassed the
  refusal append, so a destructive revert produced a receipt indistinguishable
  from a clean one. `overwritten_user_edits` is now populated on the force
  path and rendered with the paths.

### 5. What the shells get

`cli/fileview.py::undo_command` is the ONE dispatcher; both shells call it and
render its PLAIN lines (escaped on the way out). A repository path containing
`[` therefore cannot delete a message - pinned by
`TestShellParity::test_rendered_lines_carry_no_markup_delimiters`.

```
/undo                 stage the newest turn (a second one WIDENS the range)
/undo code|task|all   set the granularity of the staged range
/undo plan            describe what committing WOULD do; writes nothing
/undo commit          apply it now
/undo discard         drop the range without reverting
/undo force           apply, and record every user edit it overwrites
/undo <file>          unchanged: the historical per-file revert
```

`UNDO_SCOPE_ALIASES` maps the words a PERSON types (`code`, `task`, `all`) onto
the canonical scopes. Without it, typing `/undo code` would fall through to the
per-file engine and try to revert a FILE called `code` - a real defect the
prompt's own argument hint would have shipped.

**A new prompt commits the staged revert** (`commit_staged_undo_for_prompt`),
placed after every bare session command and slash command and before the agent
dispatch in both shells, so `repo`/`model`/`help`/`/whatever` never silently
revert code. ONLY a `files` range auto-commits: a `conversation` or `both`
range rewrites history and has to be typed.

### 6. The fallback that is easy to get wrong

**A bare `/undo` with NO captured turn hands the line back to the historical
per-run engine.** A session that predates (or misses) the capture seam would
otherwise become un-revertible the moment this feature shipped. Two existing
tests (`test_cli_slash2.py::TestTuiNewSlashes::test_undo` and
`test_cli_terminal_parity.py::test_model_skill_connector_and_undo_state_reuse_shared_backends`)
caught this by going red; the fix was in the product, not in the pins.

### 7. Config discipline

Six keys are read from `Task.config` with bounded module defaults:
`undo_staged_enabled`, `undo_staged_max_file_bytes`,
`undo_staged_max_snapshots`, `undo_staged_max_turns`,
`undo_staged_conversation`, `undo_staged_scope_prefixes`. **None is in
`harness/config.py::DEFAULTS`**, because a default there is merged into every
task and every eval arm and would silently switch all of them. `harness/config.py`
is R2-04's file and was NOT edited. Pinned by
`TestHonestyGates::test_no_staged_undo_key_is_in_the_harness_defaults`.

### 8. Verification actually run (this tree, `-p no:randomly`)

- **NEW `tests/test_agt_09_staged_undo.py` -> 68 passed, 1 skipped** (a Windows
  symlink-privilege case, i.e. BLOCKED coverage, not a pass). Host-only: no
  Docker, no model, no network. The mutation path in the capture tests is the
  REAL `harness.editor.apply_text_edit` over a real directory, and
  `TestEditorCaptureHooks::test_every_mutation_primitive_is_wired_to_the_capture`
  is SELF-ARMING: removing a hook from any of the six primitives fails that
  test, so the coverage cannot quietly evaporate.
- Six neighbour lanes, all green, listed in the `INTERFACES.md` Change Log
  entry. Total: 1389 passed, 5 platform skips across the lanes touched.
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0. No prompt changed,
  so this is unchanged by construction as well as by measurement.
- `python -m ruff check` clean on `memory/checkpoints.py`,
  `harness/editor.py`, `cli/fileview.py`. `cli/tui.py` and
  `cli/interactive.py` each hold ONE pre-existing finding
  (`B007` at `tui.py:8106`, `RUF010` at `interactive.py:5992`) - both in
  regions this round did not touch, and neither is on a line added here.
  `ruff format` was deliberately NOT run on any of the four shared files.
- **No Docker lane and no live-provider lane were run**, and neither is claimed.

### 9. Cross-terminal requests (NOT applied here)

1. **`harness/agent_kernel/strategy.py` and `harness/agent_loop.py` - the
   whole-tree step seams.** The capture seam is live on the MUTATION path (every
   `safe_edit` / `safe_write` / `safe_rename` / `safe_delete` /
   `safe_apply_patch` / `apply_text_edit` call captures around itself), which is
   what the feature relies on today. A step-level
   `StageCapture.begin_turn()` / `end_turn(clean=True)` pair would additionally
   record a whole-tree `step_before` / `clean_completion` snapshot per turn, and
   `end_turn(clean=True)` is what writes the `assistant_message` row the undo
   surface reads. One call site each; those files are AGT-05's and AGT-10's.
2. **`harness/config.py` (R2-04's) - optional discoverability.** Six
   `None`-valued `undo_staged_*` entries would be behaviour-neutral (an absent
   key already takes the module default), but publishing a REAL value would
   switch every task and every eval arm, and
   `test_no_staged_undo_key_is_in_the_harness_defaults` will fail if one
   appears. The `StageCapture.__init__` docstring documents each key.
3. **`cli/commands.py` - the `/undo` headless policy is unchanged.** The
   argument hint and summary were widened; the in-flight `refuse` policy, the
   `workspace:write` permission, and `result_presentation="diff"` are as they
   were, so a headless `/undo` still refuses while a run is live.

### 10. Not implemented, stated plainly

- **No GC of the object store.** Turns are pruned from the journal
  (`max_turns`) and their objects are left behind. A store that grows without
  bound is a real cost and this round did not solve it; `prune()` is the seam.
- **`turn_pruned` journal rows are written but nothing consumes them.** A
  pruned turn's objects are unreferenced, not deleted - deliberately, since a
  receipt might still name one.
- **The staged range is per session id.** Two sessions in one repository keep
  two independent ranges, and neither can widen the other's. A repository-wide
  range would need an owner decision about which conversation a revert belongs
  to.
- **`/undo plan` renders through the receipt renderer, so its lines read
  "reverted 0 path(s)"** where they should read "would revert N path(s)". The
  data is right (`status: "dry_run"`); the word is not. Cosmetic, and left
  rather than half-fixed.
- **No kernel (`SafeToolBackend`) capture hook.** A mutation dispatched
  through `execution.workspace.SafeToolBackend.execute` directly, rather than
  through one of the six editor primitives, is not captured. That file is
  another owner's and the request is the same one AGENT-03 already filed there.

---

## VEX-PF-10 - the verification gate reaches the daily path, and every verdict names its rung (2026-09-29)

**Owns: the five `verify()` call sites in `harness/core.py` (one new helper,
`_verify_rung_kwargs`), the one in `harness/agent_loop.py::_run_verify` (one
new helper, `_verify_boundary_names`), and the rung keys on the `verify` /
`baseline_verify` / `final_verify` / `agent_tests_verify` trace rows. Did NOT
touch the mint condition, any completion status, `harness/config.py`,
`harness/agent_kernel/**`, `execution/flake_gate.py`,
`execution/baseline_set.py`, `shared/types.py` or any CLI surface.**

The three dark mechanisms this round closed are documented in
`execution/AGENTS.md` (VEX-PF-10). This section is only what the HARNESS side
has to know.

### 1. `harness/core.py` - four call sites, one signature-gated helper

```python
def _verify_rung_kwargs(verify, cfg, **extra) -> Dict[str, Any]:
    """Return the rung keywords this boundary can accept, or {}."""
```

It is the `_boundary_names` rule `harness/model_client.py` already applies to
`effort` and `harness/tools.BashSession` already applies to
`cancellation_token`: a keyword is forwarded only when the resolved boundary
EXPLICITLY NAMES it. `harness/_stubs/verify.py` and every scripted double in
the suite carry the historical five-parameter signature, and handing one an
undeclared keyword is a run-killing `TypeError` rather than a degraded
receipt. An uninspectable callable gets `{}` - the historical call - because a
keyword it might reject is worse than a rung that is merely unrecorded. The
real boundary, `execution.verify.verify`, names all three, so production is
fully wired.

The four sites, and what each one passes:

| site | line region | `run_dir` / `phase` | why |
|---|---|---|---|
| pristine baseline verify | ~640 | run log dir, `"baseline"` | this is where the pre-existing failure set is RECORDED; it is the only honest place to observe the pristine state |
| final gate | ~1600 | run log dir, `"postfix"` | the run that mints the completion claim; the flake rung must be able to fire here or a flaky test is never caught |
| agent-written tests, post-fix | ~2300 | run log dir, `"postfix"` | a GATING gate (a post-fix failure poisons the attempt), so it has the same obligation as the final gate |
| per-step SUBMIT checkpoint | ~2900 | none | NOT a completion gate. It passes `rung_config` so the receipt NAMES its rung, but deliberately NOT a `phase`: recording a "preexisting" set from a mid-run checkpoint would attribute an inherited failure to the pristine tree, which is false precision |

`inner_verify()`'s new `rung_config`/`phase` are forwarded for the same reason;
it is the repair loop's cheap inner gate and the same keys apply.

### 2. `harness/agent_loop.py` - the daily interactive session

`_run_verify` was the general agent's ONE declared-test run and the only place
the interactive session touched a verifier, and it carried no configuration at
all. That is why the verification intelligence was structurally unreachable
from the path a person actually uses: the `harness/agent_loop.py` import graph
never named `execution.verify` (it goes through `harness.deps.get_verify()`),
and no rung could reach it even by path.

It now forwards `rung_config`/`run_dir`/`phase` under the same signature gate,
and the returned dict plus the `verify` trace row carry `rung`, `rungs`,
`flake_check`, `repetitions` and `observed_outcomes`. That last part is not
decoration: `flaky: false` is ambiguous on its own - it means both "we checked
and it was stable" and "we never checked" - and `cli/tracelog.py` renders this
row, so the disambiguation has to travel with it or an unchecked run is
rendered as a stable one.

The DONE-branch mint at `agent_loop.py:1915-1917` still reads exactly
`verification.get("target_passed")` and `not verification.get("flaky")`. The new
keys are additive and no assignment of `True` was introduced, so
`completed_unverified` is still never `completed_verified`.

### 3. The trace rows, and why

`baseline_verify`, `final_verify`, `agent_tests_verify` and the per-step
`verify` row each gained `rung`, `rungs`, `flake_check`, `repetitions` (and
`observed_outcomes` on the final gate). A reader asking "which mechanism
claimed this run was verified?" now answers a trace row instead of guessing
from the surrounding code. The keys are ADDITIVE, and they are read with
`getattr(..., default)` so a result from a boundary that does not set them
still logs the historical shape.

### 4. Handoff to 01 (not applied; those files belong to Prompt 01)

- **`cli/tui.py` / `cli/tracelog.py` / `cli/runview.py` - one new event
  vocabulary gap.** The unified `verification_rung` trace row and the new
  `rung`/`rungs`/`flake_check` keys on four existing rows are unmapped. The
  same `EVENT_VOCABULARY` gap AGT-02, AGT-10 and AGT-11 each filed for
  `reflection`, `steering_batch_boundary` and the other five; this is one more
  row for the same one-line fix. Until then a live TUI run renders the new row
  as an unmapped kind.
  `tests/test_module_reachability.py::RECORDED_UNREACHED["cli.models"]` is the
  open half of this: the model-picker state machine has no importer until a
  Prompt-01 surface names it.
- **`harness/approver.py` - still unwired, still dark.** The verifier rung now
  reaches the agent loop; the APPROVER does not. AGT-07's request stands
  unchanged and `cli/interactive.py` / `cli/tui.py` are the surfaces that
  would take it.
- **`harness/agent_kernel/completion.py:97`** still reads
  `int(self.config.get("baseline_reruns", 1))`. Not this round's file.

### 5. Verification actually run (this tree, `-p no:randomly`, real Docker)

- `tests/test_ceiling08_verification.py tests/test_ceiling_r2_02_flake.py
  tests/test_ceiling_r2_05_baseline.py tests/test_module_reachability.py` ->
  **166 passed** (151.68 s) - the required lane.
- `test_verify` + `test_verify_js` + `test_verification_gate_wiring` +
  `test_feedback` -> **120 passed** (312.16 s).
- `test_e2e_run_task` -> **28 passed** (397.01 s). This is the lane that
  matters for this file: the whole verified-fix loop through a real container
  against the modified `harness/core.py`.
- `test_agent_loop` + `test_agent_loop_matrix` + `test_config_trace_state` +
  `test_stubs_and_deps` -> **181 passed, 2 skipped** (107.15 s). The two skips
  are the `cancel` row's real-adapter runs, which need a real in-flight
  interrupt; AGT-11 records them as covered elsewhere. BLOCKED, not passes.
- `test_modes` + `test_recovery_steering` + `test_cli_runview` +
  `test_cli_tracelog` -> **308 passed** (75.40 s).
- `test_agt_02_reflection` + `test_agt_10_batch_boundary` + `test_evals_run` +
  `test_evals_tasks` -> **80 passed** (50.52 s).
- `python -m evals.run --check` -> **14/14 CLEAN**, exit 0. No prompt changed.
- `ruff check` clean on `harness/core.py` and `harness/agent_loop.py`;
  `compileall` clean. **No formatter was run over either file** - both are
  shared dirty files with four other terminals in flight, and AGT-11's
  protocol says my own hunks are hand-formatted, not swept whole-file.
- **No live-provider lane was run** and none is claimed; no credential was
  inspected, requested, or retained.

## VEX-CS-08 - `/skills` and `/agents` as verbs, with the token cost MEASURED

**Files this round owned and edited:** `harness/skills.py` (append-only region:
token measurement, supporting files, the merged catalogue, visibility),
`extensions/skill_policy.py` (the role-profile ceiling), `runtime/subagents.py`
(the cost/effort view), NEW `cli/skill_catalog.py`, NEW
`tests/test_skills_command.py`. **`cli/tui.py` and `cli/commands.py` were read
and NEVER written** - the brief declared them off-limits. `cli/interactive.py`
was read and not written. **No `INTERFACES.md` contract, event kind, journal
field, exit code or verifier mint changed, and no `harness/config.py`
`DEFAULTS` key was added.**

### 0. Read this first - the surface is built, unit-proven, and NOT MOUNTED

`cli/skill_catalog.py` implements both verb surfaces and the keyboard list.
**No line of `cli/interactive.py`, `cli/tui.py` or `cli/commands.py` calls
any of it**, so `/skills list` and `/agents list` do not yet answer in a
session and the no-argument `/skills` list does not yet open. The mount points
are in section 8 and are filed, not applied. Every statement below is about
the MODULE, not about a surface a user can reach.

### 1. The three numbers, and what they are a measurement OF

Measured on this host with `progressive_disclosure_report` (divisor
`harness.agent_kernel.budget.CHARS_PER_TOKEN`, a characters/token
heuristic, not a tokenizer - the receipt says so):

| fixture | catalogue at startup | same set with every body preloaded | saving | supporting files held on demand |
|---|---:|---:|---:|---:|
| 3 skills, 3 bodies, 6 supporting files | **32 tokens** | **3 632** | **3 600 (99.12 %)** | **7 212 tokens, 0 loaded** |

The last column is the half that matters for honesty: `preloaded_tokens` is
`catalogue + bodies` ONLY, and supporting files are never folded into it. A
report that counted them would inflate the saving, so the arithmetic is
asserted rather than the keys
(`test_supporting_files_are_never_part_of_the_preloaded_figure`).

`saving_ratio` is `None` when nothing would be preloaded, because a ratio with
a zero denominator is not a percentage anybody should act on.

### 2. Progressive disclosure is ENFORCED, not merely measured

- **Catalogue = name + description only.** `estimate_tokens` divides by the
  budget authority's own `CHARS_PER_TOKEN` rather than a second literal.
- **Bodies load on demand**, through `resolve_support_file`, which is
  **contained**: a path outside the skill root, an absolute path, or a
  traversal is refused and the row carries `loaded: False` plus a reason. A
  support-file reader that could read `../secret.md` would turn progressive
  disclosure into a file-disclosure primitive.
- **Visibility is a discovery-time ceiling**, not a row filter: a hidden
  skill is not in the catalogue at all, so no listing can show the model
  something it would not receive. `discover_skills(hidden_names=...)` accepts
  the list and also loads the persisted store by default, so a ceiling nobody
  passes explicitly is still enforced.

### 3. The visibility store, and the one-way door it used to be

`visibility_store_path` / `load_visibility` live in **`harness.skills`**, not
here: the ENFORCING reader is the scan path and `harness` may not import
`cli`. This module writes it. The store is under the **Neo home**, keyed by
repository, so a preference can never dirty a working tree, and a document
with an unknown `version` is **refused whole** rather than half-applied -
half-applying a visibility store silently re-exposes skills a user hid.

**The defect this round's own tests found:** `save_visibility([])` used to
return `(False, "nothing to hide")`, so unhiding the LAST skill wrote nothing,
every later save that removed a name was silently discarded, and the skill
came back hidden on the next session with no error anywhere. Visibility was a
one-way door. An empty list is now a valid, writable document; the only
refusal left is an **unusable value**, and a save consisting solely of refused
values still writes nothing and says why (`test_an_unusable_save_reports_instead
_of_writing_an_empty_document`).

### 4. Commands and skills are ONE namespace, and the SKILL wins

`command_sources()` merges flat `.neo/commands/<name>.md` templates with
`SKILL.md` packs and returns `CommandSource` rows. On a clash the **skill
wins and the collision is recorded** (`wins_over: ("command_file",)`), because
a flat file has no version, no tier, no taint review and no declared tools; if
both were reachable under one name, the weaker artifact is the one a user could
accidentally invoke. The same roots, symlink refusals and dot-directory
refusals as discovery are used, so the catalogue cannot be assembled from a
location the scanner would have refused.

**Plugin skills are namespaced (`plugin:<skill>`) and scanned SEPARATELY.**
That separation is the point and it was a real bug: `discover_skills` dedupes
on the BARE name, so a plugin skill whose name collides with a project skill is
gone before anything could namespace it - making the namespacing decorative
exactly when it is needed. `_plugin_skill_roots()` gives `command_sources` the
plugin skills independently. A plugin with an unknown identity yields the BARE
name rather than a guessed namespace: a wrong namespace is a name nothing can
invoke.

### 5. Declarations are data; the intersection is the grant

`attach_declarations(receipt, skills)` threads each RENDERED skill's own
frontmatter declaration onto the receipt. A skill the receipt never rendered
gets no declaration - declaring something that was never delivered is the same
defect class as claiming a delivery that did not happen.

`extensions/skill_policy.py` gained `role_tool_surface`,
`resolve_tools_for_role` and `explain_declaration_for_role`. The rule is that
**a declared tool is intersected with the ROLE PROFILE**, and the session
envelope is a *second, different* ceiling; both intersections are reported and
the effective set is the stricter of the two.

**A defect this round fixed:** `effective_tools` was gated on
`session_present`, so an absent session envelope - the common case for a
definition-facing surface, where no session exists yet - returned the whole
declared list while `refused` named the tool. A record whose refusal names a
tool that its effective set still contains is the "it says no and reads yes"
shape the composition exists to prevent. It is now the stricter of the two,
always. `_declaration_tools_of` also reads `declared_tools` OR `tools`, because
a `Skill` carries the first and an `AgentDefinition` the second, and reading
only one produced an empty declared list that reads as "this asked for nothing".

### 6. `/agents`: a cost view, and the effort pair kept apart

`AgentCostView` renders model tier, effort, tools, budgets, version and source
as PLAIN lines. Three decisions:

- **`model_tier` is a class of MODEL; `effort level` is a parameter ON one.**
  They are different questions and `tier_requested_effort` is the single
  translation (`cheap -> low`, `medium|expensive -> medium`, unknown/absent ->
  `auto`), deliberately in the SAFE direction: a tier never implies a level
  above `medium`, which is the ladder's floor. `cli.skill_catalog.agent_effort_for`
  is a thin **delegation** to `runtime.subagents.agent_effort`, not a second
  copy - two implementations of one translation is how a renderer and the
  runtime come to disagree about what effort an agent asks for.
- **`level` and `honoured` are separate fields AND separate lines.** The
  receipt also carries `effort_requested_level` and `effort_tier`, because
  `effort_level` is what the MODEL will use (`auto` when nothing is sent) and a
  receipt reading "medium" beside `honoured: false` reads as a medium call that
  quietly did nothing.
- **`max turns: the session default` for a non-positive cap.** 0 turns means the
  agent must not run at all, and rendering that as an inherited working budget
  reports a definition nobody can use as one that quietly got a limit. The
  view cannot tell an ABSENT key from a declared 0, so the raw number stays in
  `to_dict()`.

**Two parser defects fixed:** `model-tier` and `max-turns` were read only in
their underscore form, so an idiomatic Markdown definition got a SILENT
`medium` tier and a ZERO turn budget - and every surface rendered both as "the
session default", a value nobody chose. **And agent enable/disable is a
filesystem move on `<name>.md.disabled`**, the same convention the skill layer
already uses; the earlier `with_suffix` derivation produced a name the loader
does not read. `enable` resolves the tier from the MARKER on disk, because a
disabled agent is by definition not loadable and a resolver that trusted only
the roster would refuse the exact agent it exists to re-enable.

### 7. The keyboard list ACTS, and the receipt cannot lie

`SkillBrowser` is a pure state machine (`LIST_WIDGET_ID == "neo-skills-list"`,
`SKILL_KEYS` declared beside it so a mount finds both).

**`d` and `e` now move files.** They previously set `message = "disable <x>"`
and returned a word for a caller to act on - a keystroke that looks like it did
something and did not, which is the same shape as a gate rendering `pass`. They
call `cli.plugins.disable_skill` / `enable_skill`, which own the rename, refuse
a plugin-origin row with the reason (a plugin's skills are switched off by the
PLUGIN's marker, not by renaming a file inside a bundle), and say which tier
acted. The pinned proof asserts the FILESYSTEM, not the return word.

`skill_receipt_for()` used to call `build_skill_receipt(scan,
model_content=True)` - forcing a delivery claim for a projection. `model_content`
is now derived from the block the scan actually rendered, so
`render_skill_receipt` prints `harness.skills.NONE_MATCHED` ("(none matched)")
for a scan that matched nothing. The placeholder has exactly ONE literal in the
tree, counted over EXECUTABLE string literals by AST (docstrings excluded, so
this module's own prose about the placeholder is not what makes the test pass).

Every line is plain text with two exits: `escape_lines` (rich's own escaper) and
`safe_lines` (`rich.text.Text`, no parser at all). The pinned proof renders a
hostile skill name `[bold red]evil[bold]` through a REAL `Console` and asserts
the text is still VISIBLE - and the fixture carries no `/` in the name, because
a slash in a folder name is a path separator and a fixture that cannot reach
its own subject measures nothing.

### 8. Handoff to 01 - `cli/interactive.py` / `cli/tui.py` / `cli/commands.py` (FILED, NOT APPLIED)

| what | the name to use |
|---|---|
| the keyboard list widget id | **`skill_catalog.LIST_WIDGET_ID == "neo-skills-list"`** |
| the declared keys | **`skill_catalog.SKILL_KEYS`** (up/down, `t`, space, `d`, `e`, enter, escape) |
| one keystroke | **`SkillBrowser.key(name) -> str`** (never raises; unknown key returns `"ignored"`) |
| the plain lines | **`SkillBrowser.lines()`** -> `escape_lines(...)` / `safe_lines(...)` |
| both verb surfaces | **`skills_command(verb, rest, *, repo_path, config, home)`** and **`agents_command(verb, rest, *, repo_path, model, roots)`** -> `{"ok", "verb", "lines", "payload"}` |

Four mounts, all additive, all in files this round did not open:

1. **`cli/commands.py` - registry rows.** `/skills` is one row today with a
   free-text argument. It needs rows for `list enable disable inspect create`
   (`skill_catalog.SKILL_VERBS`) and a NEW `/agents` row with
   `agent_catalog.AGENT_VERBS` (`list show inspect create enable disable`).
   `opens_browser_without_argument` on `/skills` is how the no-argument list
   opens instead of printing; every other verb RETURNS, which is the same
   discipline Terminal 07's `/mcp` recorded.
2. **`cli/interactive.py::skills_subcommand`** - replace the body with a
   delegation. A bare `/skills` (no verb) must return
   `{"result_kind": "browser", "widget_id": LIST_WIDGET_ID, "keys": SKILL_KEYS,
   "lines": ...}` rather than a listing; a bare `/agents` is `list`.
3. **`cli/tui.py`** - mount the list, and route `SkillBrowser.key` to it. The
   module imports neither `cli.tui` nor `cli.commands` and never emits markup,
   so the mount is the only place a screen appears.
4. **Nothing else needs changing.** A `lexecute_type()`-style caller can also
   reach this through `neo run`, but that is a script surface and this round
   does not add a flag for it.

### 9. Verification actually run (this tree, `-p no:randomly`, `PYTHONIOENCODING=utf-8`)

- **NEW `tests/test_skills_command.py` -> 89 passed, 2 skipped.** Host-only: no
  Docker, no provider, no network, no credential. The two skips are the Windows
  symlink-privilege cases, **not passes**.
- **REQUIRED lane** `test_skills.py test_ceiling12_hooks.py
  test_agent_kernel.py test_terminal08_daily_truth.py test_skills_command.py`
  -> **221 passed, 4 skipped** (3 22 s, isolated `NEO_HOME`/`NEO_GLOBAL_ROOT`).
  `test_ceiling12_hooks.py` was measured BEFORE the `user_hooks.py` outage below;
  the same lane minus that file, re-run at the end on the final tree, is
  **185 passed, 3 skipped** (5 26 s). Without isolation `test_skills.py`'s two
  "no skills found" cases are FALSE on this machine, because a `demo` plugin
  skill is installed globally.
- **Neighbour lane** `test_agent_kernel.py test_terminal08_daily_truth.py
  test_skills_command.py test_extensions.py test_ceiling06_orchestration.py
  test_skills.py` -> **239 passed, 4 skipped** (8 26 s).
- `python -m ruff check` -> **All checks passed** on all five owned files.
- **NOT run and NOT claimed:** no Docker lane, no live-provider lane, no
  `python -m evals.run` matrix (this round changed no prompt), no full-suite run.
  No credential was inspected, printed or retained.

### 10. Two reds, attributed and NOT counted as passes

1. **`tests/test_orchestration.py::TestOrchestratorSubprocess::test_child_wall_-
   timeout_retries_and_resumes`** failed in a 4-file combined run and **passed
   standalone** (1 passed in 10.02 s). It is the host-load class
   `runtime/AGENTS.md` records: the child imports `litellm` in ~15 s against
   the test's `max_workflow_wallclock_s=15.0`, and four terminals were editing
   the tree throughout. No assertion was weakened and no timeout was added.
2. **`extensions/user_hooks.py` could not be imported for part of this round**
   - `IndentationError: unexpected indent` at line 2910, inside an unfinished
   edit to `save_trust_document` (an extra indent on the `try:` after the lock
   path is built). **Another terminal's untracked in-flight file; NOT edited
   here.** `tests/test_ceiling12_hooks.py` could not collect while it stood.
   Every lane above that does not import `user_hooks` is green on the final
   tree, and the required-lane number above was measured BEFORE it appeared.

### 11. Not implemented, stated plainly

- **Nothing is mounted** (section 0). `/skills` and `/agents` do not answer in
  a session yet; the browser does not open.
- **The derived receipt carries declarations, but the COMPILED bundle does
  not.** `harness/knowledge.py::KnowledgeContext.skill_receipt` reads the
  bundle's sections and source references; those do not carry parsed
  frontmatter, so a caller can thread declarations onto the receipt it holds
  (`attach_declarations`) but cannot get them from the compiler. That is
  Terminal 08's original filed gap, narrowed rather than closed, and it is the
  knowledge owner's call whether the skill section carries them.
- **No visibility ceiling is exposed as a `Task.config` key by default.** The
  seam is `skills_hidden` read by key PRESENCE
  (`test_a_visibility_ceiling_is_readable_from_task_config_by_key_presence`),
  and a test asserts no `skill_visib*` or `skills_list*` key ever appears in
  `harness/config.py::DEFAULTS`: a default there merges into every task and
  every eval arm, and "which panes this person has open" is not a fact about a
  run.
- **No `d`/`e` on a flat command file, and none on a plugin-origin row** -
  both are refusals with reasons, not silent no-ops.
- **`_tier_for` resolves the project tier before the global one**, matching
  discovery precedence, so a name present in both resolves the same way twice.
- **No pagination, no filter field, no mouse.** The list is a keyboard state
  machine; `up`/`down`/`t`/`space`/`d`/`e`/enter/escape is the whole vocabulary
  and `SKILL_KEYS` is the declaration a shell reads.
- **No `evals.run` arm and no Docker lane.** Nothing here needs either and
  neither is claimed.

### 12. Shared dirty tree

Nothing was reset, cleaned, checked out, restored, stashed, rebased, staged,
committed, pushed, tagged or published. **No VCS action of any kind.**
`cli/tui.py` and `cli/commands.py` were read and never written.
`cli/interactive.py`, `cli/plugins.py`, `cli/models.py`,
`extensions/user_hooks.py` and `runtime/roles.py` were being edited by other
terminals throughout and were not touched. Every edit here is additive and
confined to an appended region of `harness/skills.py`, the appended regions of
`extensions/skill_policy.py` and `runtime/subagents.py`, and two new files;
the exact symbols are enumerated in `logs/command-surface/terminal-08.json` so
a re-base can find them. `logs/command-surface/` is gitignored, so the JSON is
local evidence - **a re-base needs this section, not the JSON.**

---

## P1/W1-T1 — retrieval stops being the thing you wait for (2026-10-02)

**Round:** Phase 1 Daily Usable, Wave 1, T1 (harness). **Phase objective:
performance, not features.** Edited `harness/retrieval.py`, `harness/agent_loop.py`,
`harness/agent_loop_step.py`, `harness/scan_mode.py`,
`harness/agent_kernel/strategy.py`, `harness/config.py`, and added FIVE new
modules + one test module. `runtime/**` (litellm), `cli/**`, `tests/**`,
`evals/**`, `execution/**`, `memory/**`, `shared/**` and `.github/**` were
**read and not written**. No verifier mint, completion status, event kind or
Boundary 0-5 signature changed.

### 0. The headline, and the number it came from

| | before | after |
|---|---:|---:|
| retrieval on THIS repo (median of 5, warm) | **144.6 s** (doctrine §8) / **90.2 s** (re-measured this round) | **0.648 s** |
| directories a single walk opens | **147,241** | **271** |
| bare `os.walk` of the repo | 526,884 entries in **241.0 s** | not on any hot path |

**The skip-set was the bug, and ripgrep was the mitigation.** One missing
directory name accounted for 99.7% of it. Measured here, one pass each, same
walk, no other change:

```
retrieval's own _SKIP_DIRS                147,241 dirs   71.64 s
retrieval's own _SKIP_DIRS + "logs"           271 dirs    0.08 s
```

`logs` is the harness's own run-artifact tree — one subdirectory per run, each
holding a `pristine/` and a `work/` **copy of the repository under repair**. A
code search was walking a copy of the answer. `harness/agent_loop.py` and
`harness/scan_mode.py` both had `logs` in their tables;
`harness/retrieval.py` — the one on the per-step path — did not. That is the
whole 345×, and it is `phases/DOCTRINE.md` §8's `_SKIP_DIRS` drift with the
specific name filled in.

### 1. `harness/skipset.py` (NEW) — ONE authority, and the set is the union

Three `_SKIP_DIRS` literals inside `harness/` are now one object. The test
asserts **object identity**, not equality:

```python
assert retrieval._SKIP_DIRS is skipset.SKIP_DIRS
assert agent_loop._SKIP_DIRS is skipset.SKIP_DIRS
assert scan_mode._SKIP_DIRS is skipset.SKIP_DIRS
```

Three equal sets pass an equality check on the day they are written and diverge
the first time one of them gains an entry — which is exactly what happened.

**Unifying on the union CHANGES RETRIEVAL SEMANTICS, and that is stated, not
buried.** A walk that previously descended into `logs/`, `site-packages/`,
`Temp/`, `graphify-out/`, `probe_logs/` or `.shots/` no longer does. This is not
only a performance decision: every one of those is another tool's output, a
third-party vendored package, or a scratch directory, and a code agent that
greps its own run logs will report its own history back to itself as source.
`harness/scan_mode.py` already documented exactly that reasoning ("cloned
fixture repos under logs/ are third-party code, not the project's"); this module
makes it the one rule. 45 directory names + 1 glob.

`skip_dirs_for(drop=, add=, reason=)` is the only way to get a variant, and it
**raises without a non-empty `reason`** — a walk that prunes differently must say
why in a receipt, which is the whole difference between this and a private
literal.

**Still a THIRD authority outside `harness/`:** `execution/walk_scope.py`
(`SKIP_DIR_NAMES`, T2's) and `memory/code_graph.py` (`SKIP_DIR_NAMES`). All three
now cover `logs`/`site-packages`, so they AGREE today; nothing enforces that, and
a fourth table is one refactor away. Cross-terminal request filed.

### 2. `harness/search_engine.py` (NEW) — locate-or-fetch, and never silent

- **Resolution order:** `PATH` → checksum-verified Neo-home cache → **opt-in**
  fetch → stated fallback. `EngineResolution` carries `(engine, path, version,
  source, reason, fetch_refused)` and **never raises**.
- **A binary is proven by RUNNING it**, not by `is_file()`. A zero-byte
  interrupted download passes a path check and then fails every search.
- **Auto-fetch is OFF by default** (`NEO_RETRIEVAL_AUTO_FETCH=1` enables it).
  This is a decision, not an omission: downloading and then EXECUTING a binary
  as a side effect of searching a repository is a supply-chain action, and this
  codebase's standing rule is that network egress is opt-in
  (`webfetch_allow_remote`, `docs_lookup_allow_remote`, `scan_remote_deps` all
  default off). The fetch IS implemented, version-pinned, and
  checksum-verified; the OFF arm is a first-class resolution with a stated
  reason, not an error. **A reader can always tell "we chose not to download"
  from "the download failed"** — different operator actions, different fields.
- **A checksum mismatch discards the archive and marks nothing usable**, so a
  corrupt cache cannot become a delayed silent fallback in a later session. The
  digest is read from the upstream `.sha256` **sidecar**, not computed-and-
  trusted: computing a digest of what you just downloaded proves only that it
  did not change in transit, not that it is what upstream published. A missing
  or unparseable sidecar is a REFUSAL.
- `ripgrep_search()` applies **our** bounds to ripgrep's output rather than
  trusting `--max-count` (which is per-file, so a thousand-file repo would
  return a thousand files each at the cap — not the same bound the fallback
  enforces). Every returned path is re-checked with `path_is_under`; "the engine
  respected our `--glob` list" is an assumption about a subprocess, and the
  check is one `os.path.realpath`.
- `ripgrep_glob_args()` emits `!**/name/**` — **the trailing `/**` is
  load-bearing**: `!**/logs/` does not exclude the directory's CONTENTS in
  ripgrep, so it would prune less than the fallback and reintroduce the drift
  this removed.

**On this host neither `rg` nor `fd` is on `PATH`**, so every measurement below
is the **python-fallback** arm, named as such in every result. The ripgrep path
is implemented, bounded, and reachable; it is not what produced these numbers,
and the receipt says so.

### 3. `search_repo` now uses ripgrep, and says which engine ran

`search_repo` is a thin shell over `_search_repo_bounded`, which decides the
**refusals once** (paging / bad pattern / bad path) so both engines enforce them
identically — a refusal only the Python path enforced would be a capability
that vanished the moment somebody installed ripgrep. The scan body then picks
the engine.

Three defects found and fixed while doing it:

- **A second, duplicate walk.** `harness/retrieval.py` had its own un-hoisted
  `os.scandir` implementation for the search path (`_iter_bounded_files`), so it
  never picked up the hoisted `PathSafety` index the retrieval path already had
  (measured at **4.97 ms/file**). It is now a thin adapter over
  `walk_code_files_budgeted`, which is THE walk.
- **`_python_scan` re-ran `_safe_source_path` per file** — the same O(depth)
  symlink check the walk had already answered. 3.075 s → 1.235 s median.
- **`walk_code_files_budgeted` gained `start=`** (keyword-only, additive) so a
  `path=`-scoped search begins where the caller asked. A `start` outside the
  repository is **refused**, not silently widened to the whole repo. The
  containment test appends the separator on purpose: a bare `startswith` accepts
  a sibling directory whose name merely begins with the root's.

### 4. The literal prefilter — a speedup that had to be PROVED sound

ripgrep prefilters on a required literal; the Python fallback now does the same.
**This is a wrong-answer risk, not a slow-answer risk**, and the brute-force
soundness check found five real unsoundnesses in successive drafts:

| construct | the bug it would have shipped | verdict now |
|---|---|---|
| `[a-z]+` | required literal `"a-z"` — a class matches ONE character | `""` |
| `(?:abc)`, `(?=x)y` | a non-capturing group / lookahead emits nothing | `""` |
| `foo\|bar`, `(a\|b)\|a?b1` | a **top-level** alternation means no literal is required | `""` |
| `a{2,3}b` | required literal `"2,3"` — a quantifier is not matched text | `""` |
| `a{,3}b` | not a quantifier to Python's `re`, and its BODY read as a run | `""` |
| `xb*`, `b{0,3}x` | a zero-minimum quantifier makes its target OPTIONAL | target dropped |

Also rejected on purpose: `a{b}c` yields `"c"`, not `"a{b"`. `re` treats the
braces as literals there, but `a{,3}b` is something else entirely, and modelling
that is not worth a two-character-longer filter. **The filter is merely shorter,
never wrong.**

The proof is a **differential**, not an assertion: 3,468 randomly generated
patterns (1,192 of which yield a filter) checked against every string of length
≤3 over a 19-character alphabet — **0 divergences** — plus a differential of
`search_repo` with the prefilter forced on and off over 14 hand-picked
patterns — **0 divergences**.

**Two measurement lessons are in the code because they cost real time:**

- The first prefilter was tested **per line against the whole body**, i.e.
  O(lines × filesize). It turned a 1.2 s scan into **26.4 s**. An
  "optimisation" that is 20× slower has to be measured, not reasoned about. The
  prefilter is now tested **once per file**.
- The prefilter is compiled with **`re.IGNORECASE`, the same machinery as the
  real matcher**, not as `needle in body.lower()`. Two reasons: `.lower()`
  allocates a second copy of every file (20 MB of garbage here), and
  **`str.lower()` and `re.IGNORECASE` do not agree on Unicode** (U+212A KELVIN
  SIGN lowercases to `k`; U+0130 lowercases to two characters). A prefilter
  using one and a matcher using the other can disagree, and when they do the
  prefilter silently drops a real match. Deriving both from one engine removes
  the class, not the instances we happened to think of.

### 5. T1.W1.4 — every search result carries its cost and its truncation status

`SearchOutcome` gained, populated on **every** path including all three
refusals: `engine`, `engine_source`, `engine_reason`, `duration_s`,
`truncated_by`, `max_results_applied`, plus `truncated` / `complete` properties
and a `cost()` method. `to_dict()` flattens them (additive only — no existing key
moved, renamed, or changed meaning).

`truncated_by` is a **closed vocabulary naming the bound** — `matches` / `files` /
`max_results` / `""` — and `""` is the ONLY value that may be presented as a whole
answer. `truncated` is deliberately **not** `not ok`: an over-cap search is a
refusal *and* a truncated result, and "can I treat this as the whole answer" is a
different question from "did it succeed".

**Two truncation routes, both now loud.** The over-cap route always was
(`TOOL ERROR [too_many_matches]`). The route that was **silent** is a
successful search narrowed by `max_results`: it now renders
`[TRUNCATED: max_results] this is a partial list.` A partial list that reads as a
whole answer is the specific dishonesty the cost receipt exists to prevent, and
each route has its own test plus a **control** that an untruncated result is
still reported complete — without that control, "always says TRUNCATED" would
satisfy the truncation test.

### 6. T1.W1.2 — the turn caps

**`agent_max_turns`: 25 → 60**, justified in a 25-line comment at the key with
the spend implication stated (worst-case ceiling 25 → 60 model calls, 2.4×;
expected far smaller because most tasks finish inside the cap; the real
protections are `budget_cap_usd` and `max_wallclock_s`, enforced independently).

**THE CALIBRATION FINDING NO PROMPT ANTICIPATED, and it is the important one:
`agent_max_turns` is a PROMPT INPUT, not only a bound.**
`harness/prompts.py::CONTEXT_REINJECTION_TEMPLATE` renders
`Turn: {turn} of at most {max_turns}`, and
`harness/agent_kernel/strategy.py::_reinjection` feeds this key into it. So
25 → 60 **changes a prompt**. AHEAD measured system-prompt prose alone at
−2.3pp, so `python -m evals.run` is the gate for that line and not a comment.
It was run: **14/14 CLEAN**.

**Every site calibrated to 25** (grepped, not assumed): four readers, all now
reading the one authority, and the four hard-coded literals are **DELETED
rather than updated** — a literal that still exists is a literal that can drift:

| site | was | now |
|---|---|---|
| `harness/agent_loop.py:1283` | `int(cfg.get("agent_max_turns", 25))` | `turn_caps.resolve_caps(cfg).per_task` |
| `harness/agent_loop_step.py:535` | `_as_int(cfg.get("agent_max_turns"), 25, floor=0) or 1` | `turn_caps.resolve_caps(cfg).per_task or 1` |
| `harness/agent_kernel/strategy.py:497` | `max(1, int(... , 25))` | `max(1, turn_caps.resolve_caps(self.config).per_task)` |
| `harness/agent_kernel/strategy.py:1736` → `render_context_reinjection` | `int(... , 25)` → **the prompt** | `turn_caps.resolve_caps(self.config).per_task` |
| `harness/agent_kernel/context.py:27` `MAX_TURNS = 24` | the context-summary bound | unchanged; it is the context cap, not the per-task cap |

### 7. `session_max_turns` — the brief's premise was WRONG, and that is the finding

The brief says it has "**zero readers**". It has a reader, and what the reader
does is the real defect:

```python
# harness/agent_kernel/kernel.py:470
ContextBuilder(store, max_turns=int(self.config.get("session_max_turns", 24)), ...)

# harness/agent_kernel/context.py:249
state.turns = state.turns[-self.max_turns :]
```

It bounds how many prior **context-summary records** the compiler keeps. It does
**not** bound the conversation. So the defect is not a dead key — **it is a name
that lies about what the key does**, and a cap under a misleading name is the
same failure the brief describes, wearing a comment.

**Decision, and why.** Keep `session_max_turns` (it is live and load-bearing);
do not rename it outright (that silently changes behaviour for existing configs
and for the kernel call site, and a rename is not a performance change);
**add the real conversation cap under a name that says what it is**:

- `session_max_turns` — behaviour UNCHANGED. `turn_caps` publishes
  `SESSION_MAX_TURNS_ALIAS = "session_context_summary_turns"` as the honest name
  and the receipt reports **which key supplied the value** and what it bounds.
- `session_max_conversation_turns` — the REAL conversation cap, default `None`
  (= unbounded). `None` for two reasons: it is the honest statement of today's
  behaviour, and **a real value in `DEFAULTS` is merged into every task and
  every eval arm**, so shipping one would silently truncate conversations
  project-wide on the day the key landed.
- `render_caps()` renders the absent conversation cap as **"unbounded", not
  omitted** — omission reads as "fine", which is the confusion this removes.

**The approach is an OBSERVATION, not a refusal.** `approach_observation()`
returns a receipt within `turn_cap_approach_warn_turns` (10) of a cap and
`None` otherwise, and the loop emits ONE `turn_cap_approaching` event per run
(latch, not per turn). It stops nothing. The shape is deliberately the doom-loop
path's `needs_input`: a cap a run is about to reach is a **fact about the run**,
and a fact should be reported, not converted into a failure. A cap that turned
into a refusal at N−1 turns would be a second, invisible cap — the exact defect
this round exists to remove.

### 8. `harness/repomap_cache.py` (NEW) — Aider's diskcache design, v4

`SPDX-License-Identifier: Apache-2.0`, upstream URL, commit pin, and the
mandatory **"WHY IT BEATS OURS"** field. Not a code copy: an independent
implementation of Aider's CACHE DESIGN against this repo's symbol records and
this repo's honesty rules. The reason it beat ours is stated in the header and is
a *latency* defect, not a *ranking* one — `memory.code_graph.load_or_build` is
content-digest-keyed but rebuilds the **whole** index when any file changes, so
on a repository under active edit its hit rate on the access pattern that
matters is ≈0. Editing one file now re-derives one file.

The four required properties, all test-pinned:

1. **Per-digest invalidation.** One row per `(rel, sha256)`. Proven: after one
   file's digest changes, `hit == PARTIAL`, `served=1, derived=1`, `hit_rate
   0.5` — a distinct value, not a fractional hit.
2. **Bounded GC that REPORTS.** `max_bytes` 256 MiB + `max_rows` 400k, LRU
   eviction, `dry_run`, and every collection returns `rows`/`bytes` removed
   plus a sentence. **A read never collects** — a cache whose lookup evicts is a
   cache where reading is a mutation. Collection happens on `put` / `put_many` /
   `stats`, and `collect()` is public.
3. **Four honest hit values.** `hit` / `partial` / `miss` / `unavailable`, and
   `unavailable` reports `honest_hit_rate is None`, **never `0.0` and never
   `miss`**. A schema-v3 cache is `UNAVAILABLE` with the reason, and a corrupt
   row is dropped rather than served (half-parsed symbols are worse than none).
4. **Cold and warm are separate fields.** `cold_s` and `warm_s` are never
   averaged. Measured: cold `miss` 0.0241 s; warm median of 5 = **0.00011 s**.

**Two API defects my own tests caught.** `_measure` reported the **pre-GC** row
count next to "evicted 7 rows" — a receipt that contradicts itself. And
`collect()` reused `rows` for both "rows in the store" and "rows removed", so a
**dry run reported six evictions it had not performed**; the keys are now
`rows_in_store` / `rows` and a dry run reports `rows: 0`. A dry run that claims
an eviction is worse than no dry run.

### 9. Verification actually run (this tree, `-p no:randomly`)

- `pytest tests/test_retrieval_tools.py tests/test_agt_04_search_budgets.py -q`
  → **69 passed, 2 skipped** (9.06 s). The 2 skips are Windows symlink cases,
  BLOCKED coverage, not passes.
- `pytest tests/test_agent_kernel.py tests/test_agent_loop.py
  tests/test_config_trace_state.py -q` → **123 passed** (135.64 s).
- `pytest tests/test_context_budget_engine.py tests/test_context_budget_rerank.py -q`
  → **32 passed** (86.79 s).
- `python -m evals.run --check` → **14/14 CLEAN**, exit 0. **This is the gate
  for the `max_turns` prompt change** (§6) and it is green.
- `python -m ruff check harness` → **6 findings, all pre-existing and in files
  this round did not touch**: `_stubs/model_router.py` (1),
  `agent_kernel/kernel.py` (1), `docs_lookup.py` (4). Same six the P0-W2-T1
  handoff recorded. Every file this round created or edited is clean.
- `python -m compileall -q harness` → **exit 0**.
- `git diff --check -- harness/` → **exit 0**.
- **NEW `harness/test_p1w1_performance.py` → 47 passed** (3.13 s). And
  `pytest harness/ -q` → **147 passed, 0 errors** — the
  `test_config.py::test_config_guard` collection error P0-W2-T1 recorded as an
  open gap is **gone**, fixed by another terminal, not here.
- `pytest tests/test_scan_mode.py tests/test_ceiling_r2_09_scale.py -q`
  → **67 passed, 1 skipped** (182.74 s).
- **No Docker lane and no live-provider lane were run** and neither is claimed.
  Every model call in these suites is a scripted double.

### 10. Retrieval timings — 5 runs each, medians, cold and warm SEPARATE

`python -m evals.run`-independent; driver at `Temp/opencode/measure.py`.

```
'def search_repo'    median= 0.778s  samples=[1.327, 0.814, 0.764, 0.778, 0.768]  files_scanned=881
'_SKIP_DIRS'         median= 0.469s  samples=[0.486, 0.453, 0.429, 0.497, 0.469]  files_scanned=793
'TOOL ERROR'         median= 0.707s  samples=[0.667, 0.731, 0.710, 0.707, 0.677]  files_scanned=668
'cache_hit'          median= 0.589s  samples=[0.605, 0.543, 0.526, 0.589, 0.637]  files_scanned=627
OVERALL MEDIAN        0.648s        engine=python-fallback
```

**Cold vs warm, stated separately and never averaged:** cold (first search in a
fresh process) **1.327 s**; warm median **0.648 s**. The warm figure is **not**
quoted as the cold cost anywhere.

### 11. Not implemented, stated plainly

- **The ripgrep path is implemented, bounded and reachable, but UNEXERCISED on
  this host** — neither `rg` nor `fd` is on `PATH`, and auto-fetch is off by
  design (§2). Every number above is the **python-fallback** arm, named as such
  in every result. A host with `rg` installed will get the fast path; that
  claim is **not measured here** and is not made.
- **Auto-fetch is off.** Enabling it is one env var and downloads a
  version-pinned, checksum-verified binary into the Neo home. Not done by
  default because it executes a downloaded binary; see §2.
- **`harness/repomap_cache.py` has NO production call site yet.** The mechanism
  is complete and test-pinned; wiring it into `rank_symbols`'s extraction stage
  is an integration, and doing it inside this round would have meant changing
  the ranking path with no way to attribute a regression. **Nothing in this
  round pretends otherwise.**
- **The three authorities are still three tables** — `harness/skipset.py`,
  `execution/walk_scope.py`, `memory/code_graph.py`. They agree today; nothing
  enforces it.
- **`fd` is resolved and `fd_find_files` exists but has no call site.** The
  Python walk is the one discovery path, and it is the one that was too slow.
- `harness/retrieval.py` contains a **pre-existing U+FFFD corruption** on the
  old `_LARGE_FILE` line (a mangled em-dash from an earlier PowerShell edit). The
  line was rewritten by this round's edit, so it is gone from there; a
  repo-wide scan for `U+FFFD` is **not** done and other occurrences may remain.
- **Unifying the skip-set changes which files are retrievable.** That is the
  intended direction (§1) and it is a retrieval-QUALITY change as well as a
  performance one. It is stated here and in `skipset.py`'s docstring; it has not
  been A/B'd for retrieval quality, only for time.

### 12. Cross-terminal requests

1. **T2 (`execution/`) and T4 (`memory/`) — converge the three skip tables.**
   `harness/skipset.py::SKIP_DIRS` (45+1) is now the harness authority;
   `execution/walk_scope.py::SKIP_DIR_NAMES` (T2's) and
   `memory/code_graph.py::SKIP_DIR_NAMES` are the other two. All three now cover
   the load-bearing names, and `execution/walk_scope.py` already has a
   `missing_from()` drift detector that will report `()`. **Ask:** pick ONE home
   for the set and have the other two import or re-export it. `execution/` and
   `memory/` may not import `harness` in the direction T2's file implies
   (`memory` is below `harness`; `execution` is not), so the likely answer is a
   `shared/` module that all three import — **which is not a file this round
   may create.**
2. **T3 (`runtime/`) — the `agent_max_turns` value you may have calibrated
   against.** `harness/agent_loop.py`, `harness/agent_loop_step.py` and
   `harness/agent_kernel/strategy.py` read it; all four now go through
   `harness.turn_caps.resolve_caps`. `harness/agent_kernel/context.py:27`'s
   `MAX_TURNS = 24` is a **different** cap (context-summary records) and is
   unchanged. If anything in `runtime/` is tuned to 25 turns, it needs re-checking.
3. **T4 (`cli/`) — the live rail and `/status` need the two receipts, which now
   exist and are stable:**
   * retrieval cost: `harness.retrieval.SearchOutcome.cost()` →
     `{engine, engine_source, files_scanned, matches_returned, duration_s,
     truncated, truncated_by, complete, files_not_searched, cap}`. Additive on
     `to_dict()` — no existing key moved.
   * turn caps: `harness.turn_caps.turn_caps_receipt(config, turn=n)` →
     `{caps: {per_task, context_summary, conversation}, approach_warn_turns,
     notes, rendered, turn, approaching, approach_observation}`. The
     `rendered` field is a single line naming every cap, with the conversation
     cap shown as **"unbounded"** when unset.
   * The run already emits **`turn_cap_approaching`** (once per run, with
     `caps` attached). **This is a new event kind** and `cli/runview.py` does not
     classify it — that is the same `EVENT_VOCABULARY` gap AGT-02, AGT-10 and
     AGT-11 each filed for `reflection` and the steering kinds. One more row
     for that one-line fix. It is also **not** in `shared/tracing.py`'s overlay.
4. **T5 — two test requests.**
   * **Rung 8 (50+ turns).** Both cap values are observable via
     `turn_caps.turn_caps_receipt`. Suggested assertions: `per_task >= 50`; the
     `turn_cap_approaching` event fires before the cap is reached; a run that
     reaches 50 turns is **not** silently stopped; `session_max_conversation_turns`
     is reported as `null`/unbounded rather than absent.
   * **Rung 9 (fast enough).** `SearchOutcome.cost()` carries `duration_s` and
     `engine`. Suggested assertion: retrieval `duration_s < 1.0` on the repo
     under test, **and** `engine` is present — plus a `truncated is False or
     truncated_by` assertion, because a fast truncated result is not a passing
     one.
   * **`harness/test_p1w1_performance.py` is `harness/`-local and is NOT
     collected by `pytest tests/`.** Selection:
     `python -m pytest harness/test_p1w1_performance.py -q` (47 passed, 3.1 s).
5. **T5 / `INTERFACES.md` owner — a Change Log entry belongs here.** No
   Boundary 0-5 signature, event kind (except the additive
   `turn_cap_approaching`), serialized field, exit code or verifier mint
   changed. Additive surface: NEW `harness/skipset.py`, `harness/search_engine.py`,
   `harness/turn_caps.py`, `harness/repomap_cache.py`; `SearchOutcome` gained 6
   additive fields + `cost()` + 2 properties; `walk_code_files_budgeted` gained
   the keyword-only `start=`; `DEFAULTS` gained `turn_cap_approach_warn_turns:
   10` and `session_max_conversation_turns: None` and changed
   `agent_max_turns` 25 → 60 (which **changes prompt prose**). This round did
   not edit `INTERFACES.md`; it is not a file in this slot.
