# Phase 5 brief — Developer Experience

**Phase:** 5 · **Written by:** T5 · **Date:** 2026-10-06
**Predecessor:** `docs/p4-g4.md` (G4 = `pass`, with the limits in §0 of that document)

Phase 5 is the largest surface phase this project has attempted. It is also the
first phase whose deliverable is **what a person sees**, and that changes the
definition of evidence: almost every claim Phase 4 made was established by
counting bytes and reading source, and almost every claim Phase 5 will make is
established by a person looking at a screen.

---

## 0. The one thing to carry forward

Phase 4's exit gate passed, and its most valuable finding was a **negative** that
took three rounds to obtain:

> `read_before_edit` costs 2 turns per feature and ~7% more tokens, and the
> `subagent_isolation` mechanism compressed **nothing** because the largest child
> transcript was 25 characters against an 8,000-character cap.

**Phase 5 inherits that shape.** The mechanisms of Phase 3 and 4 are built,
declared, justified, pinned — and most of them have never been **exercised by a
workload large enough to matter**. Every one of them was measured at a scale where
it could not show a difference.

So the first thing Phase 5 should do is **not** add surfaces. It should make the
existing mechanisms visible enough to be *hated*, and then find the scale at which
they start to matter.

---

## 1. Scope — the seven surfaces

Phase 5 is the TUI diff pane, git UI, vision, keymap, LSP, subagent steering, and
the session/feature surfaces Wave 1 built. In dependency order, because three of
these are blocked on something else landing first:

| # | surface | blocked on | the honest first question |
|---|---|---|---|
| 1 | **Session & feature surfaces** (the `cli/session_briefing.py`, `cli/feature_ledger.py`, `cli/memory_provenance.py`, `cli/lineage.py`, `cli/worktree_view.py` files Wave 1 landed) | nothing — **start here** | which of these are reachable in a session, and which are code nothing calls? |
| 2 | **TUI diff pane** | the ledger's audit-trail fix (§4 of G4) | does the pane render `history.jsonl`, or only `features.json`? |
| 3 | **Git UI** | the diff pane | does a rendered diff distinguish a run's own write from a user's? |
| 4 | **Keymap** | nothing | is every advertised key bound to a real action? |
| 5 | **Subagent steering** | ~~`TurnPolicyPipeline` being driven~~ — **unblocked**: T3's 61-test middleware proof landed mid-wave, so the ordering is now exercised. The remaining dependency is the **kernel-import conflict** (§2.1 of G4). | what does steering reach that is not in the parent's window? |
| 6 | **Vision** | a real provider | **cannot be completed** — see §5 |
| 7 | **LSP** | `strategy.py`'s LSP wiring landing | does a cleared diagnostic reach the model as a fact? |

**Item 6 is blocked on credentials, and item 5's blocker is now a decision
rather than a missing test.** Starting the others anyway produces surfaces that
cannot be evaluated, which is the Phase-1 lesson
(`harness/repomap_cache.py` is 500 lines, pinned, and has **zero** call sites).

---

## 2. The evidence standard changes, and this is the hard part

### 2.1 Bytes stop being the evidence

Phase 4 could say "the gate fired once per session" and be done. Phase 5 cannot
say "the diff pane works" and be done.

**A UI claim is established by a rendered artifact, not by a return value.** The
precedent is already in this repo: `evals/session_journey.py` drives the **real
`cli.tui.NeoApp`** through a Textual Pilot, captures **SVG + transcript**, and
hashes every artifact into a `manifest.json`. Two full runs of the same tree
report `identical: true` across six scenarios and 143 typed lines.

**That harness already exists and Phase 5 should extend it rather than invent a
second one.** Four fixes it made are worth not re-learning: the sliding tail
(93 of 111 diff hits were a 4000-character window moving, so the tail left the diff
and the delta became the signal), lost wrap space, card chrome, and a repaint race
(a capture taken the instant a step returned missed rows a frame later).

### 2.2 The anti-vacuity rule gets harder, not easier

`DOCTRINE.md` §3: *"A test that can pass vacuously is worse than no test."* A UI
test has three new vacuity channels Phase 4 never had:

1. **The screenshot proves nothing was rendered.** A blank frame hashes fine.
   → assert a **marker from the surface under test** is in the capture.
2. **The pane renders an error and the test asserts "no crash".** → assert the
   success path's content, and assert the error path's content **separately**.
3. **A keymap test passes because the key is documented.** → assert the
   **action fired**, with an observable effect, not that a binding exists.

`evals/prompt_pin.py` already solved the same class for prompts, by pinning
prompts to **arms that would detect a change** (`CONTROL_ONLY_ARMS`) rather than
to strings. **The UI analogue is: pin a keymap entry to the scenario that
exercises it.**

### 2.3 What "done" means for a Phase 5 surface

A surface is done when:

- its render is in an `evidence/bundles/` capture, hashed;
- a **diff** between two runs is empty for unchanged behaviour, and the diff
  **names the added text** when it is not;
- a **negative** test exists — the surface with the data removed must render a
  stated empty state, never a blank;
- the **plain-lines** renderer is asserted, not the Rich markup. `evals/`
  recorded a defect where a hostile skill name `[bold red]evil[red]` was invisible;
  the safe path is a `rich.text.Text` with no parser at all.

---

## 3. The specific traps this codebase has already paid for

| trap | what it cost | the rule |
|---|---|---|
| **A screenshot is not evidence** | 93/111 diff hits were window movement | diff the **delta**, never the tail |
| **Card chrome is not content** | box-drawing rules shifted column widths | normalise box drawing; assert verdict **words** survive |
| **A repaint race** | a capture missed rows a frame later | settle the pilot (200 ms + two pauses) before capturing |
| **An empty `results[-0:]` is everything** | `clearing.protect_newest=0` protected every message and reported `skipped` | a silent no-op that reports success is the exact failure class |
| **Marking a `__pycache__`-era artefact** | a stale bytecode file served a turn ceiling for a whole session | there is already a pin for this: `tests/test_config_defaults_match_source.py` |
| **Host-dependent markup** | `.claude/skills` on the operator's machine made two "no skills found" tests false | repository content travels; operator-machine content does not |

---

## 4. The highest-value item, and it is not a surface

**`docs/p4-g4.md` §4.2 says it, so it is repeated here:**

> `features.json` is a **status board**; `history.jsonl` is the **audit record**.
> A reader who looks only at the first sees a green ledger after an eight-session
> thrash and cannot tell.

**The single most valuable Phase 5 surface is one that renders the ledger's
history, not its status.** A "session/feature" view that shows
`who · when · why · on what evidence · ran by` turns an integrity mechanism
from a file format into something a person can audit.

It also **fixes the sharpest remaining honesty gap in the ledger**: `apply_status`
records the *actor* and the *evidence's session*, and after the P4/W2 drift fix
they cannot disagree silently — but nothing shows a person that they did.

**Concrete ask:** the feature ledger view's default is the **history**, and the
status board is a summary of it.

---

## 5. What Phase 5 cannot complete, and should say so now

**Vision (item 6) is blocked on credentials.** `harness/vision.py` is built,
pinned, and produces a real provider-native content block for all three API
shapes. **No live provider is reachable in this checkout** — T3 recorded
`ServiceUnavailableError: No available channel` on 3/3 live completions.

So a Phase 5 vision surface can be **built and pinned**, and its end-to-end
behaviour is **`blocked`**. Ship it that way, with the reason attached to the
surface rather than to the release notes.

**Two structural limits vision must not paper over**, both already pinned:
`Qwen2.5-VL` is multimodal and is **refused** (the text-only hint is
one-directional *by construction* — a hit can only add a refusal, never grant a
capability); and **image bytes are never pattern-matched** — only the path and the
metadata go through redaction.

---

## 6. LOC and lane budget

The blocking `smoke` lane is **already over its budget** (recorded at 96.3 s
against ≤ 1 min), and it was measured on a contended host, so treat that as a
lower bound. **Widening a lane that is already over is the budget owner's
decision, not a side effect of adding a surface.**

So Phase 5's default should be `host-only`, with promotions to `smoke` proposed
separately and each one carrying its measured cost. **P4/W1 asked for three
promotions and got zero, deliberately.** The same discipline applies.

Sub-30-second structural pins (AST over one module, a pure render) can go to
`smoke` as they land. Anything that starts a session, a container, or a provider
does not.

---

## 7. The three questions Phase 5 should end up able to answer

A gate is a question that can be asked and answered without a human present.
Phase 5's three:

1. **"What did it do, and why?"** — the history-first ledger view (§4), rendered,
   diffable, and answerable from the capture alone.
2. **"Did it change anything I would not have changed?"** — the diff pane
   distinguishing a run's own write from a user's, with git output that says which.
3. **"Is a key advertised but unbound?"** — every advertised key bound to a real
   action, and the binding's absence a **loud** failure rather than a no-op.

**Each is currently unanswerable**, and each maps to one of the seven surfaces.
That mapping is the phase.

---

## 8. Cross-terminal requests owed to Phase 5, from Wave 2

| owner | file | what | why it matters here |
|---|---|---|---|
| **T1** | `harness/agent_kernel/tools.py:433` | `read_before_edit_report` does **not** forward `self.read_before_edit_refusals` to the module-level `read_before_edit_report(..., refusals=...)` | the registry's own receipt reports `paths_refused: 0` after a gate that genuinely refused. One line. A UI reading that receipt would show a guard that did nothing. |
| **T1** | `harness/agent_kernel/read_before_edit.py` + `tools.py::_note_run_mutation` | converge the path vocabulary (`source_path` in one, not the other) | G4 gap #1; a one-line fix |
| **T3 + T4** | `harness/agent_kernel/strategy.py:29` vs `tests/test_ledger_store_structural_pins.py` | T3's kernel import of `runtime/turn_middleware` **breaks** a pinned invariant; the test is RED | **two pins disagree and only a human can pick.** Subagent steering (surface 5) depends on which way it goes |
| **T3** | `runtime/turn_middleware.py` — **WITHDRAWN, closed mid-wave** | ~~nothing constructs `TurnPolicyPipeline`~~ — T3 landed `runtime/test_p4w2_middleware_proof.py`, **61 tests green**, driving the real `build_pipeline(...)` with a control. Pin and registry entry deleted. | steering now has something to be measured against. **What is still missing is the quality delta**, not the test: nobody has run the middleware-on and middleware-off arms. |
| **T3** | `runtime/subagents.py:755`, `:761` | `summarize_child` still cuts at `[:400]` and never sets `truncated` | the render path is honest (`:701`); the construct path is not |
| **T2** | `harness/long_horizon.py` `EndToEndReport` | no regression or flake term | G4 §3; assessment is that it will not close in P4 |

**T5 edited none of these files.** Changing another terminal's pin to make a
conflict disappear is how a red gets laundered instead of resolved.

---

## 9. Definition of Done

- [ ] Each of the seven surfaces has a **rendered artifact** in `evidence/bundles/`, hashed
- [ ] Each has a **diff** against a prior run that is empty when behaviour is unchanged
- [ ] Each has a **negative** test: the empty state is stated, never blank
- [ ] Every advertised key is **bound to an action**, and the binding's absence fails loudly
- [ ] The ledger view's **default is the history**, not the status board
- [ ] Any surface blocked on credentials or another terminal ships labelled `blocked`, with the reason on the surface
- [ ] `smoke` promotions are **proposed with their measured cost**, not taken by default
- [ ] `python -m evals.run --check` **14/14 CLEAN** (the gate for any prompt or context change)
- [ ] `python -m evals.ci_lanes` **CI_LANES_CONSISTENT**
- [ ] **No live-model claim anywhere.** Every render is from a scripted boundary, and says so

---

## 10. What no prompt anticipated, from the wave that wrote this brief

Three things a Phase 5 planner should know, because none of them is a surface and
all three will shape how surfaces get judged:

1. **An arm that cannot separate is worse than no arm.** P4/W1 shipped four arms
   and two were void — their mechanism keys were read by nothing on the path a
   session took. The report looked complete and measured nothing.
   **For Phase 5: assert it ran.** A UI "arm" that renders a fixture with no live
   data is the same defect wearing a costume.

2. **A receipt that says "nothing happened" when something did is the exact
   failure class.** Found twice in one wave: the read-before-edit registry
   receipt reporting zero refusals after the gate fired, and the initializer
   silently creating a `PROGRESS.md` in an arm that has no initializer — which
   then counted as a changed file in the metric. **For Phase 5: a rendering that
   reports "0" must prove it looked, not that it found nothing.**

3. **The measurement has to be at a scale where the mechanism can matter.** The
   `subagent_isolation` arm ran correctly and compressed nothing, because the
   child transcript was 0.31% of its cap. A green measurement of an unstressed
   mechanism is not a weak result; it is **no result**, and the only honest
   report is the one that says so. **For Phase 5: before shipping a surface,
   measure the case it exists for.** If that case is rare, say the surface is
   rare.