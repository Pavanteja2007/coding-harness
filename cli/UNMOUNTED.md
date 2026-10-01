# `cli/UNMOUNTED.md` — the surfaces that were built, proven, and never applied

**Owner:** T4 (`cli/**`). **Produced:** P0 / Wave 1, T4.W1.4.
**Purpose:** make P1.2 mechanical. **Nothing in this file was mounted.**

> ## STATUS — P1 / WAVE 1, T4 (2026-10-02): 7 of 9 MOUNTED
>
> **This file is a historical record. Do not read it as the current state.**
> The inventory below was measured against the tree as it stood before
> Wave 1 and every number in it has since moved. The machine-readable
> version of the current state is **`cli/test_mount_inventory.py`**, which
> measures the mounts rather than describing them — so it cannot go stale
> the way prose can.
>
> | # | surface | state now | where |
> |---|---|---|---|
> | 1.1 | `cli/review.py` | **MOUNTED** | `tui.py::VexApp._review_surface`, `interactive.py::_render_review_surface` — `/diff` shows the real review surface, with per-hunk verdicts and a hash-verified restore |
> | 2.1a | multi-instance guard | **MOUNTED** | `tui.py::VexApp._acquire_instance_guard`, `headless.py::_resolve_session` — two `vex` on one repo are REFUSED (proved against a real second OS process) |
> | 2.1b | `session_pulse` | **MOUNTED** | `tui.py::_session_pulse` → the rail's `session` section; `interactive.py::_print_session_pulse` → `/status` |
> | 1.4a | `command_aliases` | **MOUNTED** | `resolve_line` at all THREE resolution sites (TUI dispatcher, REPL dispatcher, REPL reader thread) |
> | 1.4b | `command_queue` | **MOUNTED** | `tui.py::self._command_queue` is a real `CommandQueue`; `self._queue` is a write-through view, not a second store |
> | — | tri-state toggle | **FIXED** | `toggles.ToggleSettings.flip` — the stall was reading `spec.default` instead of the live value. The TUI's workaround is deleted. One vocabulary. |
> | — | retrieval display | **ADDED** | `runview.retrieval_projection` / `retrieval_lines` — the run view had NO retrieval axis and NO truncation renderer at all |
> | 1.2 | `cli/palette.py` | **reason-unmounted** | Two palettes would disagree about what the product can do. Owner: whoever reconciles the data source. The 6 keyboard tests are recorded HANGING on this host. |
> | 1.3 | `cli/models.py` | **reason-unmounted** | One line in `cli/main.py` plus a screen class and a keybind; `cli/main.py` was not this round's file. |
>
> ### Four things the round found that this file did not predict
>
> 1. **The tri-state toggle's cause was NOT the one recorded here or in
>    `cli/AGENTS.md`.** Both said `flip` indexed `TRISTATE_VALUES` while
>    `coerce` canonicalised to `show`/`hide`. On the tree this wave
>    received, `flip` already read `spec.values` — and still stalled. The
>    actual cause was one token: `flip` read `spec.default` instead of the
>    **live** value, so the proposed next value was always the same one.
>    **Fixing the vocabulary alone would have left the toggle dead** — and
>    §7 of `cli/AGENTS.md` had already recorded a vocabulary migration as
>    the fix.
> 2. **"The default daily path gets no pre-apply diff" was not true.**
>    `build` is the only mode that can edit and it already declared
>    `approval="ask"`, so `agent_approval` was already `require`. The real
>    gap was structural: the gate was keyed on a **separately-declared
>    word**, so a new editing mode that forgot it would silently get no
>    diff. It is now derived from capability, with the opt-out explicit.
> 3. **The run view had no retrieval axis and no truncation renderer at
>    all** — not a wrong one, an absent one. §2.7 below records
>    `cost_projection` as "8 test refs, 0 surface refs"; the same was true
>    of retrieval, and it was not in the inventory because it was never
>    built rather than never mounted.
> 4. **`cli/tui.py::_queue` could not simply be retyped.** Making it a
>    derived read is the obvious edit and it is a **trap**: a derived list
>    accepts `append` and discards it, so a caller that believes it queued
>    a command has been lied to. It is a write-through view over the one
>    `CommandQueue` authority, which keeps every existing call site honest.
>
> ### The inverted pins this round FLIPPED (for T5's registry)
>
> | pin | old claim | new claim |
> |---|---|---|
> | `tests/test_daily_platform_parity.py` `test_no_shell_calls_the_guard_yet` | `"session.open_session(" not in source` | `test_the_shells_now_hold_the_single_writer_guard` — asserts the mount |
> | `tests/test_design_layout.py` `test_the_sidebar_mode_vocabulary_is_the_toggle_registrys` | two vocabularies + a two-way translation | ONE vocabulary, and the legacy spellings are aliases that are never written |
>
> Both were flipped **in the same change that mounted the surface**, as
> their own docstrings instructed. The remaining pins in the table below
> still assert what they asserted and were not touched.

**Everything below this line describes the tree BEFORE P1/Wave 1.**

Read `phases/DOCTRINE.md` §6 before acting on any row:

> **A dead guard is worse than an absent one.** If you build a mechanism with no
> call site, either wire it this phase or file a named, test-pinned handoff.
> Never leave it silently inert.

Every row below is a dead guard. Every row names the pin that flips when it is
mounted.

---

## How this was measured — and the trap in the method

Naive name counting is **useless** here and produced 30+ phantom "mounts" on the
first pass. `escape_lines`, `safe_lines`, `main`, `connect`, `decide`,
`restore_checkpoint` and `classify_failure` each exist in six or more modules.

The counts below are **binding-precise**: a reference counts only when the
referring file has a live alias bound to that module (`from cli import review as
_review` → alias `_review`) or did `from cli.review import symbol`. Two further
corrections matter:

- `from cli import X` is **not** `from cli.X import Y`. An early AST pass that got
  this wrong reported *zero* importers for `cli/review.py` while
  `cli/commands.py:1863` has a live lazy `from cli import review as _review`.
- **Module-level reachability is not symbol-level mounting.** Eight of the twelve
  modules below are import-reachable from a dispatch root and still contain large
  unmounted interiors. `cli/review.py` is the dangerous case: it *is* in the
  import graph, so any import-graph gate reports it live forever while the entire
  review surface stays dark.

Verified directly (commands and output in the round's Handoff):

```
=== cli/review.py production callers ===
commands.py:1863: from cli import review as _review
commands.py:1865: names = tuple(_review.diff_review_verbs()) or names
        ^ the ONLY production call into a 2,667-line feature

palette          : total=1 prod-excluding-self=0
models           : total=1 prod-excluding-self=0
command_queue    : total=0 prod-excluding-self=0
command_aliases  : total=1 prod-excluding-self=1
        command_queue.py:50: from cli import command_aliases as _aliases
        ^ transitively dead: its only importer is itself an orphan
```

---

## The headline

| metric | value |
|---|---:|
| public symbols across the 12 modules audited | **494** |
| reachable from a dispatch surface, per symbol | **80** |
| **UNMOUNTED (no call site on any dispatch surface)** | **414** (83.8 %) |
| unmounted **and** covered by ≥1 test (proven-but-dark) | **254** |
| unmounted **and** zero test coverage (dark-dark) | **160** |
| modules with **zero** production importers | **4** |
| `xfail` markers anywhere in `tests/` | **0** |
| recorded handoff rows across 12 rounds | **24** |
| …discharged | **0** |

| module | public | dispatch-reachable | unmounted | of which test-covered |
|---|---:|---:|---:|---:|
| `cli/plugin_runtime.py` | 74 | 0 | **74** | 37 |
| `cli/auth.py` | 59 | 2 | **57** | 40 |
| `cli/session.py` | 81 | 25 | **56** | 34 |
| `cli/fileview.py` | 61 | 24 | **37** | 20 |
| `cli/review.py` | 29 | 1 | **28** | 17 |
| `cli/palette.py` | 28 | 0 | **28** | 17 |
| `cli/command_aliases.py` | 24 | 0 | **24** | 14 |
| `cli/command_types.py` | 24 | 7 | **17** | 11 |
| `cli/models.py` | 26 | 0 | **26** | 19 |
| `cli/a11y.py` | 43 | 17 | **26** | 19 |
| `cli/onboarding.py` | 27 | 4 | **23** | 12 |
| `cli/command_queue.py` | 18 | 0 | **18** | 14 |

**Dispatch surfaces** used as the mount criterion: `cli/main.py`, `cli/tui.py`,
`cli/interactive.py`, `cli/commands.py`, `cli/command_exec.py`, `cli/headless.py`.

---

# TIER 1 — four modules the product cannot execute at all

**4,334 lines.** `tests/test_module_reachability.py:1-24` names the failure mode in
its own docstring: *"a passing test that calls the module directly is not evidence
either: the module was reachable FROM A TEST and from nothing else."*

### 1.1 `cli/review.py` — the review surface the product is missing

| field | content |
|---|---|
| **Artifact** | `cli/review.py` (2,667 lines). `build_review` **:1063**, `review_command` **:2554**, `render_review` **:1585**, `review_lines` **:1468**, `review_text_lines` **:1558**, `revert_paths` **:1846**, `changed_files_indicator` **:2181**, `record_decision` **:1681**, `blast_radius` **:2357**, `REVIEW_WIDGET_ID` **:125**, `diff_review_verbs` **:2540**. |
| **What it does** | Hash-verified, concurrent-edit-safe diff review: what the run did to your files, can I put it back, and how do I know. |
| **Why it was built** | VEX-PF-05 (`cli/AGENTS.md` §"VEX-PF-05"). 97 tests, measured at 36 ms / 1 file and 4.8 s / 200 files. `AGENTS.md` §9: *"the review surface the product is missing"* is this round's words, not mine. |
| **Where it should mount** | `cli/tui.py:4835` `if cmd in ("/diff",):` — beside the existing historical diff branch. REPL twin: `cli/interactive.py:6965`. Live-scope indicator: the TUI repaint timer (cf. `set_timer` at `cli/tui.py:7551`). |
| **Mount shape** | `document = review.build_review(task_dir, repo, config=config, width=self.size.width)` → `rendered = review.render_review(document, width=..., expanded=...)` → `self.push_screen(_ReviewScreen(rendered))` (your screen class; widget id `review.REVIEW_WIDGET_ID == "vex-review"`). Result shape is `fileview.undo_command`'s, so a shell that already renders an undo receipt renders this one without learning a second protocol; `handled=False` means "not mine" — fall through to the existing `/diff undo`. |
| **Blast radius** | `/diff` is a **mutating** surface (`accept` / `reject` / `revert`). `cli/AGENTS.md` §5 records a real gap: `/diff` has one permission tuple, so `/diff revert` and `/diff reject` would act under `journal:read` + `workspace:read` with **no `workspace:write`**. Mounting the verbs makes that gap live. Mount `show` first; the mutating verbs need the per-verb permission fix in `cli/commands.py` (filed, not built). |
| **Blocking** | (a) the per-verb `required_permissions` gap above; (b) `cli/tui.py::_slash_command_impl` has no `/plugins` branch at all, so the TUI/REPL dispatch-parity AST pin (`tests/test_cli_terminal_parity.py`) must be satisfied for `/diff` too; (c) `render_review` is the only renderer with **zero** references anywhere, including tests — it is the least-exercised seam in the module. |
| **Inverted pin** | none for `/diff` itself. `cli/AGENTS.md` §9.7 is the record: *"nothing in `cli/tui.py` calls this module yet."* |

### 1.2 `cli/palette.py` — the `/` command menu

| field | content |
|---|---|
| **Artifact** | `cli/palette.py` (1,727 lines). `open_rows` **:1341** (the entry point), `palette_entries` **:859**, `group_entries` **:909**, `search_entries` **:987**, `entry_row` **:1081** / `entry_markup_row` **:1147** / `entry_text_row` **:1174**, `palette_lines` **:1225**, `palette_receipt` **:1238**, `slash_hook` **:1304**, `PALETTE_TRIGGER` **:1301**, `unassigned_commands` **:286**, `open_rows` **:1341**, `MAX_VISIBLE_ROWS` **:1371**. |
| **What it does** | The ONE authoritative `/` menu: 56 registry commands grouped into 7 human groups, plus custom commands, plugin commands and MCP prompts, ranked by task phrasing. |
| **Why it was built** | VEX-CS-03 (`cli/AGENTS.md` §"VEX-CS-03"). Measured: correct command at **rank 1 for 30/30 task phrasings (100 %)** vs **2/30 (6.7 %)** for the pre-round matcher; ~0.3 ms per menu open. `AGENTS.md` §0: *"**No line of `cli/tui.py` calls `cli.palette`.**"* |
| **Where it should mount** | `cli/tui.py:7935` `VexApp.on_key` — immediately before the `Input` consumes the key. |
| **Mount shape** | `rows = _palette.open_rows("", context=self._command_context())` → `screen = _palette.PaletteScreen(rows, context=...)` → `self.push_screen(screen, self._palette_menu_chosen)` → `self._handle_line(value)`. Widget ids: `#palette-box`, `#palette-input`, `#palette-list`, `#palette-hint`. Dynamic rows merge from a worker thread via `screen.add_entries(...)`. Runnable snippet already written: `logs/command-surface/terminal03_mount_snippet.py` (gitignored — the AGENTS.md section is the durable record). |
| **Blast radius** | **Two palette screens.** The live ctrl+p palette is `_PaletteScreen(CommandPaletteFrame)` at `cli/tui.py:9355`, fed by `VexApp._palette_entries` at `cli/tui.py:8040` → `cli/commands.py::command_palette_entries` at `cli/tui.py:8047` — a **different data source**. Mounting `cli.palette` without reconciling them produces two menus that disagree about what the product can do, which is worse than one. The existing screen subclasses the same `CommandPaletteFrame` and reads the same registry projection, so the reconciliation is a data-source swap, not a second screen class. |
| **Blocking** | the palette/ctrl+p reconciliation above. `palette.screen` mounts are documented (in `cli/AGENTS.md` §8.1) to HANG under `textual.pilot` on a loaded four-terminal host — the same class recorded in four earlier rounds. Cadence it, don't block the UI thread. |
| **Inverted pin** | none in `tests/`; the record is `cli/AGENTS.md` §"VEX-CS-03" §0 and §7. `tests/test_palette_discovery.py::TestTheKeyboardPath` (6 tests) is currently **NOT RUN** on this host for the Pilot-hang reason — mounting it does not flip a pin, but it does make those 6 tests reachable. |

### 1.3 `cli/models.py` — the model picker and the effort variant

| field | content |
|---|---|
| **Artifact** | `cli/models.py` (1,863 lines). `register_models_parser` **:1759**, `cmd_models` **:1722**, `ModelPicker` **:1016**, `model_catalog` **:285**, `effort_variant` **:472**, `cycle_effort` **:502**, `carry_effort` **:525**, `select_model` **:812**, `model_id_verdict` **:667**, `VARIANT_CYCLE_KEYBIND` **:131**, `PICKER_WIDGET_ID` **:92**. |
| **What it does** | `vex models [provider] [--refresh] [--json]` — pick a model, and carry the effort level across the change as a *chained variant* rather than a second screen. |
| **Why it was built** | VEX-PF-03 (`cli/AGENTS.md` §"VEX-PF-03"). 98 tests. Measured: one whole frame (visible + sections + lines) at **0.082 ms median**; two real frame-budget defects found and fixed by measurement (6× win at a 2,000-row catalog). `AGENTS.md` §8: *"**`vex models` is not mounted in `cli/main.py`** … The command, its parser, its `--refresh` / `--json` behaviour and its exit codes are implemented and tested through a real argparse parser; the one-line mount is another owner's file."* |
| **Where it should mount** | **Two places.** (a) `cli/main.py:3952` — the exact sibling seam already proven by `cli/auth.py`: `_auth.register_connect_parser(sub)` sits there with the comment *"ONE additive line: the subparser and the handlers it dispatches to are declared together in `cli/auth.py`, so the command surface cannot drift away from the backend it names."* (b) `cli/tui.py:2017` `BINDINGS` block + a new `action_model_picker`, for the widget and `variant.cycle`. |
| **Mount shape** | (a) `from cli import models as _models` + `_models.register_models_parser(sub)` — **one line**. (b) `_models.ModelPicker(_models.model_catalog(self._settings()), current_model=self._model, current_effort=self._state.get("effort", "auto"))` → `self.push_screen(self._mount_model_picker(picker))`; on enter use `picker.choose()`, whose `ModelSelection.variant` carries the effort in force — **one result, not a second dialog**. |
| **Blast radius** | **`cli/main.py:3952` is the ONLY `register_*_parser` call site in the whole file**, so mounting here is additive and cannot reorder an existing subparser. For the TUI half: the picker must NOT let a completion path read `EffortVariant` — a budget hint a verifier can read is how `completed_unverified` becomes promotable. `tests/test_model_picker.py::TestTheVerifierGateIsIdenticalAtEveryLevel` pins that by a tokenized source scan of `harness/agent_loop_step.py` and `harness/agent_kernel/completion.py`. |
| **Blocking** | nothing for (a). For (b): `cli/tui.py` has **no** `action_model_picker` (0 hits), so the binding, the action and the screen class are all new. Escape the lines (`models.escape_lines` / `models.safe_lines`) — a provider name is data. |
| **Inverted pin** | none. `cli/AGENTS.md` §"VEX-PF-03" Handoff table is the record. |

### 1.4 `cli/command_queue.py` + `cli/command_aliases.py` — one queue decision and one alias authority, for three surfaces

| field | content |
|---|---|
| **Artifact** | `cli/command_queue.py` (844 lines): `decide` **:297**, `CommandQueue` **:468**, `is_exempt` **:178**, `exempt_commands` **:140**, `statusline_facts` **:757**, `queue_lines` **:786**, `queue_depth` **:736**. `cli/command_aliases.py` (860 lines): `resolve_line` **:747**, `expand_command_line` **:657**, `resolve_alias` **:283**, `validate_alias_table` **:427**, `deferred_alias_promotions` **:227**, `alias_disclosure_lines` **:791**, `MAX_STACKED_COMMANDS` **:486**, `MAX_ALIAS_HOPS` **:321**. |
| **What it does** | One queue decision (`idle → run`, `exempt → run`, registry policy honoured, `refuse` → refuse, else queue) and one alias/stacking resolution, replacing three divergent hand-rolled answers. |
| **Why it was built** | VEX-CS-04 (`cli/AGENTS.md` §"VEX-CS-04"). Measured before/after: aliases **8 → 15**; stacking **did not exist → cap 6**; queue surfaces **1 of 3 → one `decide()` for all three**. `AGENTS.md` §0: *"the alias table, the stacker and the queue are **built, unit-proven, and inert**: no surface calls them."* |
| **Where it should mount** | **Five anchors, all verified live.**<br>`cli/tui.py:4475` — `resolution = _commands.resolve_command_line(raw, context)`<br>`cli/tui.py:4488` — the queued branch does a bare `self._queue.append(...)` and hand-rolls the message at **:4491**<br>`cli/tui.py:7553` — `_after_run` does `nxt = self._queue.pop(0)` (0.05 s re-arm at **:7551**)<br>`cli/interactive.py:6492` — the REPL dispatcher<br>`cli/interactive.py:390` — **the reader thread's own second resolution call** |
| **Mount shape** | aliases: one-line substitution `_commands.resolve_command_line(...)` → `_aliases.resolve_line(...)` at all three anchors (rule 1 of `resolve_alias` is *"the REGISTRY wins"*, so a canonical line is byte-identically unchanged). queue: `self._queue.append(canonical)` → `CommandQueue.enqueue(...)` + `escape_lines(queue_lines(...))`; `nxt = self._queue.pop(0)` → `self._command_queue.drain(self._handle_line)`. **Do NOT add a queue to `cli/command_exec.py:333`** — a script has no boundary to drain at. |
| **Blast radius** | **`cli/tui.py:2215` declares `self._queue: List[str] = []`** — a hand-rolled list that shadows the class. Swapping the type changes the field's contract for every reader (`cli/tui.py:3632` `say("queue", len(self._queue))`, and five enqueue sites word the ack five different ways). `cli/tui.py:3632` is **already data-compatible** with `statusline_facts` (`AGENTS.md` §7 marks it *"a do-not-duplicate note"*), so that one is additive. `/diff undo` is reachable through the queue's stacking path, so a wrong `NON_STACKABLE` set changes what a typed line means. |
| **Blocking** | the `_queue` field retype; and the two deferred alias targets `/feedback` and `/rewind` do not exist in the registry, so `/bug → /feedback` and `/checkpoint → /rewind` stay recorded rather than shipped (needs one `CommandSpec` each in `cli/commands.py`, **no** edit to `cli/command_aliases.py` afterwards). |
| **Inverted pins** | **`tests/test_command_aliases.py:1454` `TestTheMountPointsAreNamedNotGuessed`::`test_the_handoff_json_records_every_mount_and_its_absence` (line 1457)** — loads `logs/command-surface/terminal-04.json` and asserts `row["applied"] is False` for every mount, so *"a mount must never be reported as applied from this file"*. **Line 1476 is the flip site.** Also `::test_this_round_edited_neither_of_the_two_files_it_may_not_touch` (line 1480) — an AST pin that `command_aliases.py` / `command_queue.py` import neither `cli.tui` nor `cli.interactive` nor `cli.commands`; mounting from inside those modules **breaks it**, and the fix is to delete the pin in the same change, not to weaken it. |

---

# TIER 2 — reachable modules with large unmounted interiors

### 2.1 `cli/session.py` — the multi-instance guard, the long-session receipt, the survival report

| field | content |
|---|---|
| **Artifact** | `cli/session.py` (4,302 lines). `open_session` **:4241**, `session_instance_guard` **:4196**, `session_pulse` **:3629** / `session_pulse_lines` **:3839**, `session_survival_report` **:3999** / `session_survival_lines` **:4128**, `transcript_segments` **:3902** / `transcript_segment_lines` **:3964**, `resume_session` **:1185**, `recover_session` **:1100**, `promote_session_decision` **:2144**, and the checkpoint block **:2198–2240**. |
| **What it does** | A second `vex` on the same repository is refused (write-gated, readers stay free); a 60-turn session reports what is still in context, what it cost and what is wrong; a killed process reports exactly what survived; the transcript folds repeated turns into counted segments. |
| **Why it was built** | VEX-PF-08 (`cli/AGENTS.md` §"VEX-PF-08"). Measured: survival read **1.90 ms median** against a real `os._exit(70)` kill; 120 turns → 13 segments; `session_pulse` is a pure projection (pinned to change nothing). `AGENTS.md` §4: *"**Nothing in `cli/tui.py` or `cli/interactive.py` calls this yet, so a second `vex` is NOT yet refused in the product.** That sentence is the state, not a caveat."* |
| **Where it should mount** | `cli/tui.py:2641` and `cli/tui.py:6261` — both are `self.conversation = load_or_create(` (import at `cli/tui.py:2636`). Unmount path: `cli/tui.py:7839` `action_quit_app`. The three read-only receipts go at `cli/tui.py:4719` `if cmd in ("/status",):` and `cli/interactive.py:6656`. |
| **Mount shape** | `with _session.open_session(log_root, repo, session_id, command="vex (tui)") as conv:` replacing the `load_or_create` call; register the guard so unmount pops it. Read side only, per `AGENTS.md` §4: `session_instance_guard(repo)` at mount, gate the WRITE side around the turn. Receipts are **plain text** — escape every line, or render into a `markup=False` sink. |
| **Blast radius** | `load_or_create` is deliberately NOT gated: it is also the reader behind `/sessions`, `resume_session`, `load_latest_session` and `save_session`'s revision check, so a listing must keep working while another instance legitimately holds the repository. **Gating the WRITER is the correct scope — gating `load_or_create` breaks `/sessions`.** The refusal needs a screen class that does not exist yet. `cli/exit_codes.EXIT_CODES` already has the shape (`environment_error` 3); the choice is a UX decision P1.2 must make, not one this file makes. |
| **Blocking** | `shared/instance_guard.ConcurrentInstanceError` must be importable from `cli/tui.py` at mount time — doctrine forbids a kernel package importing `cli`, so the import direction is already correct. |
| **Inverted pin** | **`tests/test_daily_platform_parity.py:2167` `TestWhatThisRoundDidAndDidNotWire::test_no_shell_calls_the_guard_yet`** — asserts `"session.open_session(" not in source` for **both** `cli/interactive.py` and `cli/tui.py`. Its own docstring: *"When Prompt 01 wires it, this test is the one to update -- and it must be updated in the same change, not deleted."* **This is the pin the round's own brief names as the example** (`test_no_shell_calls_the_guard_yet`). Also `::test_the_session_guard_reader_is_not_a_writer` (line 2185) pins `session_instance_guard`'s signature has no `command` parameter — it must stay a reader after the mount. |

### 2.2 `cli/auth.py` — `/connect`, save-first

| field | content |
|---|---|
| **Artifact** | `cli/auth.py` (2,622 lines). `connect_interactive` **:2259**, `register_connect_parser` **:2531** (MOUNTED at `cli/main.py:3952`), `apply_active_credential` **:1042** (MOUNTED at `cli/main.py:129,131`), `verify_credential` **:1486** (**zero references anywhere**), `classify_failure` **:1238**, `load_credentials` **:891** (29 test refs), `render_status` **:2119**, `looks_like_library_leak` **:1074**, `cmd_auth_logout` **:2473**. |
| **What it does** | `vex connect` / `/connect`: save the key FIRST, verify SECOND, against a child process; then one honest sentence instead of a Python class name. |
| **Why it was built** | VEX-PF-02 (`cli/AGENTS.md` §"VEX-PF-02"). Measured: save-only path **42.7 ms median**; a real probe against a public endpoint returned an auth failure in **6,938 ms** — which is the honest cost, and the sentence a user now sees instead of `litellm.Timeout`. 110 tests. |
| **Where it should mount** | `cli/tui.py:5239` `if cmd in ("/login",):` — a `/connect` branch immediately after it. REPL twin: `cli/interactive.py:7657`. Plus `cli/tui.py:3379` (`"startup"` sidebar fact, a one-word swap) and `cli/tui.py:9635` (`onboard_prompt=True` → `False`; `_OnboardScreen` defined at `cli/tui.py:898`, pushed at `cli/tui.py:2553`). |
| **Mount shape** | `result = _auth.connect_interactive(provider_id=rest, background=True, say=lambda t: self.transcript(_auth.markup_safe(t)))`; if `result.saved`, call `self._onboard_done()`. `background=True` is load-bearing: the app must never wait on a provider. |
| **Blast radius** | **`/login` refuses in flight; `/connect` MUST NOT** — `cli/commands.py` already declares `in_flight_policy="allow"` for `/connect`, and auth is the control a person reaches for *while a run is failing*. Turning `onboard_prompt` off deletes `_OnboardScreen`, which is the OLD tests-first flow that discards a typed key on failure — that is the defect, but the deletion is a behaviour change to a live modal. The REPL mount needs the same parity treatment: `tests/test_cli_terminal_parity.py` parses both dispatcher bodies with `ast` and requires equal branch-key sets, so **a REPL-only branch is a command one shell has and the other does not.** |
| **Blocking** | nothing in the module; the screen class and the parity row are P1.2's. |
| **Inverted pin** | none. `cli/AGENTS.md` §"VEX-PF-02" §8 is the record. |

### 2.3 `cli/plugin_runtime.py` — the `/plugin` runtime

| field | content |
|---|---|
| **Artifact** | `cli/plugin_runtime.py` (2,991 lines, 74 public symbols). `run_plugin_verb` **:2935** (20 test refs, **zero** surface refs), `menu_lines` **:2582**, `require_trust` **:1816**, `record_trust_decision` **:1794** / `trust_decision` **:1777** / `trust_decision_path` **:1767**, `projected_session_cost` **:1928**, `dependency_report` **:1180** / `dependents_of` **:1254**, `read_marketplace` **:2166** … `list_marketplaces` **:2401**, `uninstall_plugin` **:2509**. |
| **What it does** | The ONE implementation of every `/plugin` verb, with a hash-verified trust gate that persists an approval, a token-cost projection, a dependency graph that refuses to half-enable a closure, and nine marketplace source types with bounded network. |
| **Why it was built** | VEX-CS-06 (`cli/AGENTS.md` §"VEX-CS-06"). Measured: **61 tokens at startup** for the shipped fixture, 1,180 for a 40-component plugin; `/plugin list` 60–62 ms median of which 20–42 ms is the digest re-hash. 71 tests. `AGENTS.md` §8: *"**The delegation in §8 and §9 is NOT applied.**"* |
| **Where it should mount** | `cli/interactive.py:4972` `plugin_subcommand` (body through **:5082**) and the `/plugins` dispatch branch at `cli/interactive.py:7834`. |
| **Mount shape** | `from cli import plugin_runtime as _runtime; return _runtime.run_plugin_verb(verb, rest, project_dir=str(repo_path) if repo_path else None, confirm=..., decide=...)` — the two confirm callbacks are injected because the loader never prompts on its own, which is what keeps it scriptable. |
| **Blast radius** | **This is the second-implementation defect the round's own brief names.** `plugin_subcommand` is currently a SECOND implementation of the same eight verbs. Delegating changes `/plugins` output text and breaks `tests/test_cli_plugins.py` pins — that is a behaviour change to another owner's tests, so it is filed rather than guessed at. `cli/tui.py` has **no** `/plugins` dispatch branch (0 hits for `cmd in ("/plugins",)`), so the TUI needs its own branch or a delegation; four new `SUBCOMMANDS["/plugins"]` rows are needed for the verbs this adds (`trust`, `verify`, `tokens`, `marketplace show`). The marketplace wording is a **required** change: it currently says *"no marketplace registry is configured in this build"*, which is TRUE in one build and a lie in another — the doctrine's *"would a reader be misled?"* test. |
| **Blocking** | the `tests/test_cli_plugins.py` output pins; the four `CommandSpec` rows. |
| **Inverted pin** | none in `tests/`. `cli/AGENTS.md` §8 of VEX-CS-06 is the record. |

### 2.4 `cli/onboarding.py` — the empty states and the first-run screen

| field | content |
|---|---|
| **Artifact** | `cli/onboarding.py` (1,215 lines). `empty_state_lines` **:453** (MOUNTED), `first_run_lines` **:942** (MOUNTED), `task_groups` **:1188** (MOUNTED), `task_search` **:1132** (MOUNTED). Unmounted: `task_index` **:742**, `task_lines` **:838**, `task_help_lines` **:867**, `next_action` **:1109**, `WHAT_VEX_IS` **:896**, `MAX_FIRST_RUN_LINES` **:935**, and 4 dataclasses (**zero references anywhere**). |
| **What it does** | Nine declared empty states with an actionable sentence and a runnable door; the first-run screen; the task corpus that makes `/help` searchable by what a person wants. |
| **Why it was built** | VEX-PF-06 (`cli/AGENTS.md` §"VEX-PF-06"). Measured: task-phrased `/help` search correct at rank 1 went **9/26 (34.6 %) → 26/26 (100 %)**; first-run lines 6 → **8**; empty-state panels that render a bare refusal **11 → 0**. 193 tests. |
| **Where it should mount** | **Seven TUI sites, all located:** `cli/tui.py:4282`, `:4361`, `:4722`, `:4923`, `:5727`, `:5785`, `:6328`. Affordance row: `cli/tui.py:2572` `VexApp._print_first_run`. |
| **Mount shape** | `for line in _tc.empty_state_lines("no_diff", width=self.size.width): self.transcript(line)` — `list[rich.text.Text]`, no markup at all. Widget id `_tc.EMPTY_STATE_WIDGET_ID == "vex-empty-state"` (one id for every state). State map: `"no diff from the last run"` → `no_diff`, `"no MCP servers/connectors configured"` → `no_connectors`, `"no run in this session yet"` → `no_runs`. |
| **Blast radius** | Every empty-state sentence keeps its **historical lowercase opening**, because four suites assert on it — capitalising them took two required-lane tests red. The affordance row must NOT go in a modal: a first-run modal some people never get past is the defect this row exists to close. |
| **Blocking** | nothing; the seven sites are literal string comparisons. |
| **Inverted pin** | none. `cli/AGENTS.md` §"VEX-PF-06" §12.1 is the record: *"**Seven TUI empty-state sites are NOT wired**."* |

### 2.5 `cli/fileview.py` — three clusters, including two near-misses

24 of 61 public symbols ARE dispatch-mounted. The unmounted 37:

**(i) the staged-undo verbs.** `stage_undo` **:2074**, `commit_undo` **:2136**, `discard_undo` **:2175**, `undo_plan` **:2116**, `staged_undo_state` **:2035**, `undo_scopes` **:1999**, `undo_scope_for` **:2252**. The dispatcher `undo_command` **:2257** **IS** mounted (`cli/tui.py:5646`, `cli/interactive.py:6025`) — but the three verbs it should call are dark, while `commit_staged_undo_for_prompt` **:2453** (mounted at `cli/tui.py:5701`) is not. **Mount:** inside the mounted `undo_command` dispatch, `cli/tui.py:5646` / `cli/interactive.py:6025`.

**(ii) `vex worktree review` — a near-miss.** `review_worktree` **:3144**, `review_scope` **:3193**, `read_git_status` **:443**, `build_file_tree` **:942**. Proven by `tests/test_cli_power_tools.py:150-196`. `cli/main.py:3697` registers `p_worktree` with **four** subparsers (`new`/`list`/`go`/`rm`) and **no `review`**. The argparse branch is one `add_parser` away; the backend and its tests already exist. **Blast radius:** read-only by construction (only `git status`/`diff`/`show`/`merge-base`/hashing), so the blast radius is one subparser row.

**(iii) editor launch — a parallel implementation.** `launch_editor` **:3073** (6 test refs) is dark while `launch_editor_detached` **:3108** **IS** mounted (`cli/tui.py:4738`, `cli/interactive.py:6806`). Two implementations of one action, the blocking one dark. **Blast radius:** mounting the blocking one changes focus behaviour (it blocks until the editor exits) — that is a UX decision, and `launch_editor_detached` is arguably the right permanent answer. **Recommendation: do NOT mount `launch_editor`; delete it or mark it deprecated.** A dead guard is worse than an absent one, and this one has a live twin.

Also unmounted and small: `restore_selection_preflight` **:2930**, `settings_effective_diff` **:3602**, `clear_file_caches` **:3693**, `diagnostic_rows` **:1718**, `diff_summary` **:1265**, `help_search` **:3560** (a **second** help search; `cli/interactive.py` has the fuzzy one — one function, not two, is the filed request).

### 2.6 `cli/a11y.py` — the accessibility half

17 of 43 dispatch-mounted (the announcement system works, `focus_order_report` at `cli/tui.py:2845`). Unmounted 26: `GLYPH_TEXT` **:326**, `glyph_text` **:364**, `file_state_codes` **:374**, `describe_file_state` **:389**, `describe_status` **:413**, `render_code` **:430**, the whole diagnostic a11y vocabulary **:472–515**, and the **pagination block** `paginate` **:738** / `page_lines` **:759** / `pager_body` **:770** / `omitted_note` **:794** / `describe_pagination` **:807** / `long_output_policy` **:819**. Three of 13 `announce_*` transitions are dark: `announce_idle` **:80**, `announce_steered` **:182**, `announce_paged` **:258**.

**Where it should mount:** `cli/tui.py:9226`, `:9239`, `:9309`, `:9313` — `_SearchListScreen` (class at `cli/tui.py:8244`) hand-rolls a pager against `_a11y.Page` and never consults `paginate` / `page_lines` / `omitted_note` / `long_output_policy`. That is the same shape as the two palettes: a shared contract with a private copy on top. **Blast radius:** `omitted_note` is what makes a bounded view honest; mounting it changes every bounded panel's last row. **Inverted pin:** none; `cli/AGENTS.md` §"VEX-TERM-UX-06" §7 is the record.

### 2.7 `cli/command_types.py` — the display half

7 of 24 dispatch-mounted, all via `cli/commands.py`. Unmounted: `type_rows` **:481** (**0 refs**), `type_label` **:471** (**0 refs**), `type_markers` **:476** (**0 refs**), `cost_projection` **:486** (8 test refs), `context_budget_names` **:527**, `migration_report_document` **:941** (4 test refs, **zero refs in `cli/`** — `cli/migration.py`, which *is* imported by `cli/main.py`, never calls it). **Where it should mount:** `cli/tui.py:3616` `_statusline_facts` for the cost column. **Blast radius:** a cost column in the statusline costs rows the rail currently spends; the anti-clutter rule (`cli/design.py::ANTI_CLUTTER_MIN_ENTRIES = 3`) applies. **Inverted pin:** none.

---

# TIER 3 — the recorded handoff ledger

`cli/AGENTS.md` carries **24 handoff rows across 12 rounds; 0 discharged.** Every
one is still open. Grouped by owner:

| round | module | recorded mount | AGENTS.md line |
|---|---|---|---|
| VEX-CS-01 | `commands.py` | `/worktree` + `/mcp` + `/skills` in the TUI. *"Both halves or neither — flipping the declaration first makes the TUI refuse a row the REPL can run."* | 2206 |
| VEX-CS-03 | `palette.py` | the `/` menu (Tier 1.2) | 1458, 1584, 1687 |
| VEX-CS-04 | `command_aliases.py`, `command_queue.py` | all 9 mounts (Tier 1.4) | 2267, 2574, 2596 |
| VEX-CS-06 | `plugin_runtime.py` | `plugin_subcommand` body (Tier 2.3) | 940, 988, 1055 |
| VEX-CS-07 | `connectors.py` | `/mcp` branch in `interactive.mcp_subcommand` → `connectors.mcp_command` | 624 |
| VEX-PF-02 | `auth.py` | 3 TUI edits + 1 REPL branch (Tier 2.2) | 3921, 3967 |
| VEX-PF-03 | `models.py` | `vex models` mount + picker widget (Tier 1.3) | 12181, 12395, 12429 |
| VEX-PF-05 | `review.py` | `/diff` review screen (Tier 1.1) | 3679, 3713 |
| VEX-PF-06 | `onboarding.py` | 7 TUI empty-state sites (Tier 2.4) | 2784, 2886 |
| VEX-PF-08 | `session.py` | 4 mounts (Tier 2.1) | 3056, 3134 |
| VEX-CS-11 | `/adopt` | the `/adopt` command | 302 |
| AGT-08 | `commands.py` | `/effort` leaks `VEX_EFFORT` into the process — **a live red in combined runs** | see §"live reds" below |

---

# INVERTED PINS — every test that must FLIP when its surface is mounted

**There are zero `xfail` markers in `tests/`.** The project's inverted pins are the
opposite construct: **assertions that PASS precisely because the surface is NOT
wired**, which fail the moment somebody mounts it. These are named here because
**a dead pin is worse than a dead guard** — it keeps asserting a falsehood about a
feature that now exists.

| # | file:line | pins | flips when |
|---|---|---|---|
| **1** | `tests/test_daily_platform_parity.py:2167` `TestWhatThisRoundDidAndDidNotWire::test_no_shell_calls_the_guard_yet` | asserts `"session.open_session(" not in source` for **both** `cli/interactive.py` and `cli/tui.py`. The round's own brief names this as the example inverted pin. | `session.open_session` is mounted (Tier 2.1). **Update in the same change, do not delete.** |
| **2** | `tests/test_daily_platform_parity.py:2185` `::test_the_session_guard_reader_is_not_a_writer` | `inspect.signature(session_instance_guard)` must have **no** `command` param. | never — the reader must stay a reader. Keep. |
| **3** | `tests/test_command_aliases.py:1457` `TestTheMountPointsAreNamedNotGuessed::test_the_handoff_json_records_every_mount_and_its_absence` (assertion at **:1476**) | `row["applied"] is False` for all 9 mounts; *"a mount must never be reported as applied from this file"*. | any of the 9 alias/queue mounts lands. |
| **4** | `tests/test_command_aliases.py:1480` `::test_this_round_edited_neither_of_the_two_files_it_may_not_touch` | AST: `command_aliases.py` / `command_queue.py` import neither `cli.tui` nor `cli.interactive` nor `cli.commands`. | either module starts importing a surface. **Mounting from inside those modules breaks it by design.** |
| **5** | `tests/test_command_surface_harness.py:1672` `test_the_mount_is_not_applied_so_the_claim_is_not_overstated` | asserts `/adopt` is **not** mounted; line 1683 says *"update this pin and the handoff"*. | `/adopt` is mounted. |
| **6** | `tests/test_module_reachability.py:333-422` `RECORDED_UNREACHED` | the reviewed import-graph backlog, 18 entries, each with a mandatory reason. **Its gate `test_no_recorded_exemption_may_be_stale` (line 455) makes the set SHRINK-ONLY** — an entry must be removed when the module gains an importer. | any recorded module gains a production importer. **Currently `cli.models` is recorded (line 348) but `cli.palette` and `cli.command_queue` are NOT — so the gate reports 9 unrecorded orphans.** |
| **7** | `tests/test_module_reachability.py:517` / `:527` | self-tests that a genuinely orphaned module IS detected and that a test-only importer does NOT count. | never — non-vacuity. Keep. |
| **8** | `tests/test_design_layout.py:302` | `pytest.fail(f"region {name!r} ({widget_id}) is not mounted: {exc}")` — a layout region with no mounted widget. | a `cli/design.py` region gains its widget. |
| **9** | `tests/test_daily_session_human.py:255` | `raise RuntimeError("not mounted")`. | the human-facing session mount lands. |
| **10** | `tests/test_agt_03_edit_lint.py:688` `test_the_codemod_path_does_not_yet_use_the_in_edit_gate` | pins that the codemod path bypasses the gate. | the codemod path routes through it. |
| **11** | `tests/test_ceiling_r2_02_flake.py:677,681` | *"The self-arming wiring pin (honest about what is NOT wired yet)"*. | the flake gate's own wiring changes. |
| **12** | `tests/test_cli_tui.py:2032` | *"Agent /diff parity + bad-input safety (no Pilot: unmounted app, …)"* — the suite comments around the unmounted review screen. | `cli/review.py` is mounted (Tier 1.1). |
| **13** | `tests/test_onboarding_surface.py:268,325` | `assert names, "the registry was unreachable; this gate cannot run"` — **vacuity guards** that are NOT mount pins; they fail if the gate stops being able to run. | never. Keep. |

### A fourth class: name-based anti-claims in `cli/AGENTS.md` itself

Not tests, but the same failure mode, and a reader will take them as current.
**Four of these five were FALSE as of P1/Wave 1 and are corrected here, in the
same change that mounted the surface — which is what this section demands of
whoever mounts a row.**

- `cli/AGENTS.md` §"VEX-TERM-UX-05" §7 — *"the 15 remaining mojibake
  occurrences"*. **Still unmeasured this round; left alone.**
- ~~`cli/AGENTS.md` §"VEX-PF-05" §9.7 — "nothing in `cli/tui.py` calls this
  module yet"~~ → **FALSE as of P1/W1.** `cli/tui.py::_review_surface` calls
  `review.build_review` and `review.review_command`. The sentence is stale
  and the fix belongs in `cli/AGENTS.md`, which T4 owns and will update in
  its own round entry.
- ~~`cli/AGENTS.md` §"VEX-PF-08" §4 — "Nothing in `cli/tui.py` or
  `cli/interactive.py` calls this yet."~~ → **FALSE as of P1/W1.** The TUI
  takes the lease in `_acquire_instance_guard`; `cli/headless.py` goes
  through `open_session`. Note the sentence is still true of
  `cli/interactive.py` specifically, which is why the TUI path was split out
  rather than a single seam.
- ~~`cli/AGENTS.md` §"VEX-CS-04" §0 — "built, unit-proven, and inert"~~ →
  **FALSE as of P1/W1** for the alias seam and the queue authority, both
  mounted. The remaining inert parts of that round (the two deferred alias
  targets `/feedback` and `/rewind`, which have no `CommandSpec` to point
  at) are still inert and are recorded as such.
- `cli/AGENTS.md` §"VEX-PF-03" §8 — *"`vex models` is not mounted in
  `cli/main.py`"*. **Still TRUE** — `cli/main.py` was not this round's
  file. The row stays in the inventory with its reason.

**These were all TRUE on the pre-Wave-1 tree** (verified for `review.py`,
`palette.py`, `models.py`, `command_queue.py`, `command_aliases.py`
above). The record has been corrected where the mount made it false, and
left alone where it did not.

---

# Two cheap wins, ranked

1. **`cli/main.py:3952` — one line.** `models.register_models_parser(sub)` beside
   the existing `_auth.register_connect_parser(sub)`. It is the ONLY
   `register_*_parser` call site in the file, so the mount cannot reorder an
   existing subparser. Delivers `vex models` — a documented, tested, argument-
   parsing-complete command — for one line. **Blast radius: one new subcommand
   name in `vex --help`.** Do this one first.
2. **`cli/tui.py:2553` — `onboard_prompt=False`.** One word, and it deletes the
   old tests-first onboarding modal that discards a typed key on a provider
   timeout — the exact defect VEX-PF-02 exists to close. **Blast radius: the
   `_OnboardScreen` push at mount; `_OnboardScreen` itself becomes deletable.**
   Pair it with the `cli/tui.py:3379` sidebar word swap (`/connect add a
   provider`) or the card teaches a command that does not exist.

Everything else in Tier 1 and Tier 2 is a **feature mount**, not a wiring fix.
Mounting nine surfaces blind would be reckless; mounting *these two* is not.

---

# What this inventory does NOT cover, and why

- **`harness/`, `execution/`, `runtime/`, `shared/`, `memory/`, `mcp_server/`**
  are not T4's. Their own `AGENTS.md` files carry their handoffs; the same
  technique applies and the gate at `tests/test_module_reachability.py` is
  repo-wide.
- **`cli/command_args.py`, `cli/command_import.py`, `cli/skill_catalog.py`** are
  also reported as orphans by the reachability gate and are NOT in the twelve
  modules audited here. They are recorded here as **known-unmeasured** rather
  than counted, because a row with no measurement in it is a guess.
- **Symbol counts are binding-precise, not exhaustive.** A symbol reachable only
  through another unmounted symbol is counted unmounted — correct for this
  purpose, and the reason `cli/command_aliases.py` reads 24/24 rather than 22/24.