# `harness/PARALLEL_HAZARD.md` — the parallel-work hazard inventory

**Owner: T1 (`harness/**`). Round: P0/W1.** Written before the next wave launches,
because the highest-severity class of bug in a shared worktree is two terminals
writing the same file — and unlike a logic defect, it produces no test failure, no
trace row and no exception. It produces a silently reverted hunk.

This file is a **hazard map, not a work plan**. It answers two questions:

1. Which files in `harness/` are read by another slot, and would a change to
   them break something outside `harness/`?
2. Which of those are *also* plausibly edited by more than one phase — i.e.
   where two terminals' file lists can intersect?

Both answers are derived from the tree, not from memory. The cross-slot import
list in §2 was produced by the command in §1; re-run it before trusting any
row, because another wave may have added an import since.

---

## 1. How to re-derive this file

```powershell
# Every harness module another slot imports, with the importing file.
$all = Get-ChildItem cli,execution,runtime,memory,evals -Recurse -Filter *.py |
       Select-Object -ExpandProperty FullName
Select-String -Path $all -Pattern 'from harness(\.[a-z_0-9]+)? import ([^\n]+)' -AllMatches |
  ForEach-Object {
    $slot = ($_.Path -split '\\')[0]
    foreach ($m in $_.Matches) {
      "{0,-10} {1,-24} harness{2} import {3}" -f $slot, (Split-Path $_.Path -Leaf),
                                                 $m.Groups[1].Value, $m.Groups[2].Value.Trim()
    }
  } | Sort-Object -Unique

# Which phases name which harness files (this is the collision signal).
Select-String -Path phases\*\W*\T*.md -Pattern 'harness/[A-Za-z_0-9/]+\.py' -AllMatches |
  ForEach-Object { $_.Matches.Value } | Group-Object | Sort-Object Count -Descending
```

The first command gives §2. The second gives the "phases that want to change
this" column in §3, and it is the number that matters: **a file named by two
different phase prompts is a collision waiting for the wave that runs both.**

---

## 2. Every cross-slot import out of `harness/`

Complete as of this round. `cli/**` and `evals/**` are the heavy importers;
`runtime/**` and `execution/**` import the contracts and the run entry points.

| harness module | imported by | what for |
|---|---|---|
| `harness.agent_loop` | `cli/interactive.py` (6 names: `agent_diff`, `classify_agent_input` ×2, `load_resume_history`, `render_agent_plan`, `run_agent`), `cli/tui.py` (3: `agent_diff`, `classify_agent_input` ×2, `render_agent_plan`), `cli/fileview.py` (`agent_diff`), `cli/session_journey.py` (`classify_deterministic`), `evals/daily_driver.py` (3: `classify_agent_input`, `run_agent`, `run_agent_legacy`) | **23 importers — the single most cross-slot-coupled module in the repo** |
| `harness.agent_kernel` | `cli/interactive.py`, `runtime/orchestration.py` (`CompletionStatus`, `RunResult`), `runtime/orchestration_worker.py`, `runtime/roles.py` (`PolicyEngine`, `ToolRegistry`, `builtin_tool_specs`), `evals/daily_driver.py` (`AgentKernel`, `RunSpec`) | the typed-kernel contract surface |
| `harness.config` | `cli/commands.py` (`EFFORT_ENV_VAR`), `runtime/worker.py` (`get_config`) | the merged-config table |
| `harness.context_compiler` | `cli/fileview.py`, `evals/daily_driver.py` | `ContextCompiler` |
| `harness.core` | `cli/deps.py`, `cli/run.py`, `runtime/run.py`, `runtime/scheduler.py`, `runtime/worker.py`, `evals/daily_driver.py` (`run_task`) | **Boundary 3 `run_task` + `TaskResult`** |
| `harness.decision_memory` | `evals/daily_driver.py` | `query_planning_decisions` |
| `harness.deps` | `cli/onboard.py`, `cli/run.py`, `evals/daily_driver.py` (3 import sites) | boundary resolution + test injection |
| `harness.docs_lookup` | `evals/daily_driver.py` | `DocsResult` |
| `harness.editor` | `cli/interactive.py` (`changed_files`), `cli/review.py` (module import) | diff / edit surface |
| `harness.intent` | `evals/daily_driver.py` | `classify_input` |
| `harness.lsp` | `cli/fileview.py` (`LspManager`, `get_diagnostics`), `evals/daily_driver.py` | language-server lifecycle |
| `harness.qa_mode` | `cli/interactive.py`, `cli/live_quality.py`, `evals/daily_driver.py` | `run_question` |
| `harness.build_mode` | `cli/interactive.py`, `cli/main.py` | `run_build` |
| `harness.research_mode` | `cli/interactive.py` | `run_research` |
| `harness.scan_mode` | `cli/main.py` (`finding_task_params`, `resolve_finding`, `run_scan`) | scan + finding handoff |
| `harness.skills` | `cli/main.py`, `cli/plugins.py`, `cli/skill_catalog.py` (3 import sites incl. the private `_global_config_root`), `cli/interactive.py`, `evals/daily_driver.py` | discovery + rendering |
| `harness.steering` | `cli/interactive.py` (2 aliases), `evals/daily_driver.py` | `SteeringBuffer` |
| `harness.test_config` | `execution/ecosystems.py` (`register_language_surfaces`, `test_config_patterns`) | the polyglot test-config seam |
| `harness.tool_errors` | `cli/fileview.py`, `cli/baseline_set.py` | classification + recovery policy |
| `harness.tools` | `cli/command_queue.py` (`extend_batch_verbs` via `as _tools`), `cli/plugins.py` (`extend_batch_verbs`) | the canonical tool catalog |
| `harness.trace` | `cli/session.py` (`TraceLogger`), `evals/daily_driver.py` (`redact_secrets`) | journal + redaction |
| `harness.retrieval` | `cli/session.py` (`retrieve_symbol`), `cli/fileview.py` (`rank_symbols`), `evals/daily_driver.py` (`retrieve_context`) | retrieval |
| `harness.webfetch` | `execution/workspace.py` (`fetch_webpage`), `evals/daily_driver.py` (`FetchResult`) | the fetch tool |

**Two private-name imports cross a slot boundary.** Both are hazards a rename
will break silently:

- `cli/skill_catalog.py` imports `harness.skills._global_config_root`.
- `harness.editor` (T1) reaches `execution.workspace._atomic_write_bytes`
  (T2) — recorded in `harness/AGENTS.md` §R2-06 request 3.

Neither is this round's to fix. Both are listed here so a rename proposal knows
what it will break.

---

## 3. The hazard table: files named by more than one phase prompt

Ordered by hazard. "Phases" is read from `phases/**/T*.md`, so it is a
statement about *prompts that name the file*, which is the collision signal.

| # | file | phases that name it | importers outside `harness/` | hazard |
|---|---|---|---|---|
| 1 | `harness/agent_loop.py` | **P0/W2 (T2, T5), P2/W1 (T1, T2, T4)** | 23 | **CRITICAL.** The 3,800-line compatibility adapter, named by three different terminals across two phases, with 23 external importers. |
| 2 | `harness/config.py` | **P1/W1, P2/W1, P4/W1 (T1)** | `cli/commands.py`, `runtime/worker.py` | **CRITICAL.** Every phase adds keys. A merged default switches every task AND every eval arm. |
| 3 | `harness/prompts.py` | **P0/W1, P3b/W1 (T5)** | none directly | **HIGH.** Prompt text is an eval arm, not an edit — any change needs `python -m evals.run` before shipping. |
| 4 | `harness/editor.py` | **P0/W1, P2/W1 (T1, T2)** | `cli/interactive.py`, `cli/review.py` | **HIGH.** Touches a private name in `execution/`. |
| 5 | `harness/tools.py` | **P0/W1, P3b/W1** | `cli/command_queue.py`, `cli/plugins.py` | **HIGH.** The canonical catalog; a new entry changes `catalog_fingerprint`, which is pinned. |
| 6 | `harness/context_compiler.py` | **P0/W1, P3a/W1** | `cli/fileview.py`, `evals/daily_driver.py` | **HIGH.** Now carries the redaction boundary — see §5. |
| 7 | `harness/trace.py` | **P0/W1, P2/W1 (T5 prompt)** | `cli/session.py`, `evals/daily_driver.py` | **HIGH.** Journal authority. A change is observable in every replay. |
| 8 | `harness/agent_kernel/legacy.py` | **P0/W2 (T1, T2, T5)** | `runtime/orchestration_worker.py` (via the kernel) | **HIGH.** Owns mint site #3. |
| 9 | `harness/agent_kernel/policy.py` | **P2/W1 (T5), P3b/W1 (T2)** | `runtime/roles.py` | **MEDIUM.** |
| 10 | `harness/agent_loop_step.py` | **P0/W1, P1/W1, P2/W2** | none directly | **MEDIUM.** Owns mint site #4; the `success`-literal pin lives on it. |
| 11 | `harness/deps.py` | **P2/W1 (T1, T2)** | `cli/onboard.py`, `cli/run.py`, `evals/daily_driver.py` | **MEDIUM.** Test-injection seams. |
| 12 | `harness/retrieval.py` | **P1/W1 (T1, T2)** | `cli/session.py`, `cli/fileview.py`, `evals/daily_driver.py` | **MEDIUM.** |
| 13 | `harness/skills.py` | **P3b/W1** | `cli/main.py`, `cli/plugins.py`, `cli/skill_catalog.py`, `cli/interactive.py`, `evals/daily_driver.py` | **MEDIUM.** |
| 14 | `harness/approver.py` | **P2/W1 (T3, T4 ×2), P2/W2 (T3)** | none directly (still unwired) | **LOW today, HIGH later** — 4 prompts name it and nothing calls it. |
| 15 | `harness/lsp.py`, `harness/coordination.py`, `harness/lint.py`, `harness/tool_errors.py`, `harness/model_client.py`, `harness/agent_kernel/{kernel,strategy,tools,subagents,conversation,checkpoints}.py` | one phase each | none or one | **LOW.** |

**Not in this table and not a hazard:** `harness/redaction.py`,
`harness/test_redaction_boundary.py`, `harness/test_secret_egress.py`,
`harness/test_completion_mint_sites.py`, `harness/PARALLEL_HAZARD.md`. New
files, imported by nobody. Safe to create; safe to edit concurrently.

---

## 4. The narrowing strategy

The goal is that two terminals editing "the same file" do so in **different
regions of it**. Four rules, in order of how much collision they remove.

### 4.1 `config.py` — phase-scoped sections

The single biggest hazard, because every phase wants it and a value in
`DEFAULTS` is merged into every task and every eval arm.

- **Every new key goes in a `# === <PHASE> <slot> ===` region, appended at the
  bottom of `DEFAULTS`, never sorted into the middle.** Two terminals adding
  keys to the same dict then touch different lines, and the merge conflict is a
  two-line adjacent-add rather than a whole-file conflict.
- **`None` unless the phase's prompt names the value.** A published value is a
  behaviour change for every run in the project. This is not advice, it is
  already pinned: `test_no_codemod_key_is_in_the_harness_defaults`,
  `test_no_edit_lint_key_is_in_the_harness_defaults`,
  `test_no_staged_undo_key_is_in_the_harness_defaults`, and
  `test_no_ecosystem_default_switches_every_run` each fail if a key is
  published with a real value.
- **`DEFAULTS` is not the only thing in the file.** `get_config`'s env-aware
  merge, `EFFORT_ENV_VAR` and the module's zero-runtime-import constraint are
  separate concerns in the same file; a phase adding a key has no business
  touching them, and a phase touching the merge has no business adding a key.

### 4.2 `prompts.py` — text is an eval arm

- A prompt string is never edited in place. **Add a new named constant, switch
  the reference, and run `python -m evals.run`** — AHEAD measured system-prompt
  prose alone at **−2.3pp**, so an unmeasured prose edit is a known-negative.
- Keep new blocks in their own constant so the diff is one added name plus one
  changed reference, never an edited paragraph two terminals are both holding.
- The section-placement discipline (everything after `## Retrieved context`, so
  T3's difficulty cut never moves) is a contract with another slot; do not move
  a marker while adding a section.

### 4.3 `tools.py` — additive catalog entries only

- **Never edit an existing `_TYPED_TOOL_SPECS` entry.** Append a new one. A
  modification to an existing entry changes `catalog_fingerprint`, which is
  SHA-256'd over every name, argument, type, alias and effect class — so it
  breaks `catalog_parity_report()` assertions in a file the editor may not know
  is affected.
- `extend_batch_verbs` is the plugin seam; a new read-only batch verb should go
  through it rather than into the base pattern.

### 4.4 The journal authorities — one helper, no second decision

`harness/trace.py` and `harness/agent_kernel/events.py` are the only two files
in `harness/` that may call the redactor, and they both go through
`harness/redaction.py`. **A third call site is a defect, not a convenience** —
it is the exact divergence `harness/trace.py`'s own module docstring records
having already paid for. This is pinned by
`harness/test_secret_egress.py::test_neither_journal_calls_the_redactor_directly`.

Practical consequence for a parallel round: if you need a new journal-side
redaction, **call `harness.redaction.redact_for_journal` from the existing emit
site**. Do not open a new writer.

### 4.5 The cross-file seams that cannot be narrowed

Two hazards have no structural fix and must be handled by ordering:

- **`harness/editor.py` ↔ `execution/workspace.py::_atomic_write_bytes`.** A
  rename on the T2 side breaks T1 at import time. It is loud (ImportError), and
  `tests/test_ceiling_r2_06_editing.py` spies on the exact private name, so the
  break is caught by a test rather than at a user's command. **Requested
  promotion to a public `atomic_write_bytes` is filed in
  `harness/AGENTS.md` §R2-06 request 3.**
- **`cli/skill_catalog.py` → `harness.skills._global_config_root`.** Same
  shape, in the other direction. Loud, and pinned by `tests/test_skills.py`.

---

## 5. Egress paths this inventory does not own

Recorded here because a reader of the harness redaction audit must not mistake
silence for coverage.

| path | status | where the decision lives |
|---|---|---|
| `scripts/lint_ratchet.py` | **OUT OF SCOPE — `scripts/` is not a slot.** It reads source lines and is a real egress path if its output ever reaches a user-facing surface. **Not edited by T1.** Cross-terminal request filed in the P0/W1 Handoff. | — |
| `cli/ui.py`, `cli/notify.py`, `cli/tracelog.py` | T4's. `cli/notify.py` already has the fail-closed `(detail withheld: …)` behaviour this round mirrors. | T4's prompts |
| `shared/security.py` | T5's, and the **single authority**. Not re-implemented. | T5 |
| `execution/**`, `runtime/**`, `memory/**`, `evals/**`, `tests/**` | Not T1's. Any harness change that needs one is a Handoff request. | those slots |

---

## 6. What a wave should do with this file

1. Before editing a file in §3, re-run §1's second command and confirm no
   *other* terminal's prompt in this wave names it. If one does, that is a
   scheduling problem, not a code problem — say so in your Handoff rather than
   editing anyway.
2. Follow the narrowing rule for that file (§4).
3. Name any new cross-slot import you create here in your Handoff, so the next
   wave's map is not stale.