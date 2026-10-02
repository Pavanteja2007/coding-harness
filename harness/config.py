"""Config handling for the harness.

All tunables come from task.config (project convention: no hardcoded
constants), with these defaults applied for anything missing. The defaults
match the project spec's Phase 1 guidance (~3 retries, cheap default model).
"""

import os
from typing import Any, Dict, Mapping

#: Where an effort level may be named when nobody put one in ``Task.config``.
#: Re-exported from the runtime authority so the CLI, the harness and the
#: router all name the same variable; duplicated as a literal here so this
#: module keeps ZERO runtime imports (the harness must stay importable with no
#: provider present, and `import runtime.model_capabilities` drags in litellm's
#: neighbourhood on a cold path).
EFFORT_ENV_VAR = "NEO_EFFORT"

DEFAULTS: Dict[str, Any] = {
    # Stopping conditions (checked in harness.core.run_task)
    "max_retries": 3,  # full attempts at the whole task, total
    "budget_cap_usd": 2.0,  # hard cap on model spend per task
    "max_wallclock_s": 900.0,  # per-task wall-clock cap in seconds
    "command_timeout_s": 120,  # per bash command inside a step session
    "verify_timeout_s": 300,  # for the verifier's test runs
    # Model / routing knobs (consumed by the model boundary / router).
    # None = let the router decide (adaptive routing needs this); an
    # explicit value in task.config pins that model for every call.
    "model": None,
    "provider": None,
    "api_key": None,
    # Difficulty feature family (R2-13). None/absent = the incumbent lexical
    # predictor; "structural" = the R2-13 repository-shape challenger;
    # "auto" = the challenger only if a HELD-OUT-validated calibration exists
    # (none ships). This key is a VALUE check, so a None entry here is
    # behaviour-neutral and switching only a PRESENT, resolvable value changes
    # which feature family predicts difficulty.
    "difficulty_features": None,
    # --- R2-13 capability gate: KEY-PRESENCE keys, deliberately NOT in
    # DEFAULTS. `runtime.model_router._capability_requested` is a KEY test
    # (like the Ceiling-14 resilience keys), because the gate can REFUSE a
    # call. DEFAULTS is merged into every task config and every eval arm, so
    # an entry here -- even `None` -- would put the key in every context and
    # switch the gate ON everywhere, silently refusing unpriced targets in
    # runs that never asked for it. "Absent" is the meaningful state.
    #   "capability_gate"             present = enforce the gate
    #   "capability_allow_unpriced"   explicit allowance for an unpriced
    #                                router-chosen target
    #   "capability_strict_tools"     tool support must be VERIFIED, not
    #                                merely undeclared
    #   "capability_tool_driven"      declare a tool-driven loop for a call
    #                                that carries no schemas itself
    #   "model_capabilities"          {model: row} / [row] operator
    #                                declarations, registered through the
    #                                real capability registry
    # Streaming (VEX-CEILING-10). stream_enabled=True makes every model
    # call a `stream=True` provider call that emits `model_delta` journal
    # rows, so a slow endpoint shows "waiting for first token" and then
    # growing text instead of a silent spinner. stream_window_ms is the
    # coalescing window: 40ms is the floor (one callback per window, so
    # frame cost is independent of token rate) and the router clamps it to
    # 500ms. The assembled response is identical either way, so this knob
    # changes liveness, not the verification contract.
    "stream_enabled": True,
    "stream_window_ms": 40,
    # Harness behavior
    "max_step_turns": 15,  # bash-command turns per planner sub-step
    "max_output_chars": 3000,  # cap on tool output fed back to the model
    "context_files_cap": 4,  # files injected into a step session's context
    "context_lines_cap": 60,  # lines per file in that injection
    "max_file_bytes": 200_000,  # refuse to read/edit files larger than this
    "test_command": None,  # e.g. "python -m pytest -x"; None = autodetect
    "target_test": None,  # e.g. "tests/test_mathutil.py::test_mean"
    "baseline_reruns": 1,  # reruns for flake detection in verify()
    "protected_paths": ["tests/*", "test_*.py", "*_test.py"],
    # R2-12 polyglot. BOTH entries are `None` on purpose, and that is
    # behaviour-neutral: a default here is merged into every task and every eval
    # arm, so a real value would switch all of them at once. `None` is what an
    # ABSENT key already means to both consumers, so adding them changes no run.
    #
    #   ecosystem                 a language NAME ("go", "java", ...) that pins the
    #                             repository's ecosystem instead of detecting it
    #                             from marker files. Honesty check first: a name
    #                             the registry does not know is an error, not a
    #                             silent "no ecosystem".
    #   ecosystem_protected_paths  the repository's per-language TEST globs, for
    #                             `harness.editor.extended_protected_patterns`.
    #                             `DEFAULTS["protected_paths"]` above is
    #                             Python-shaped and matches no Java or Go test
    #                             file, so a Maven or Go repository had NO
    #                             protected test surface until this existed.
    #                             Read it by PRESENCE; a falsy or empty value
    #                             adds nothing.
    "ecosystem": None,
    "ecosystem_protected_paths": None,
    "work_subdir": "logs",  # where logs/{task_id}/ lives (repo-relative)
    # R2-06 editing correctness.
    #
    # `require_edit_digest` is the stale-edit precondition: True refuses a
    # mutation of a file the session never read. `harness.editor.apply_text_edit`
    # - the one exact-block edit primitive this round built - is STRICT when the
    # key is ABSENT or None, so every mutating path that goes through it is
    # already on by default. That strictness needs no default value: it is the
    # primitive's own fail-closed floor, and it is pinned by
    # tests/test_ceiling_r2_06_editing.py.
    #
    # The value here is deliberately None rather than True, and the reason is
    # MEASURED, not stylistic. The kernel reads this same key
    # (agent_kernel.tools.ToolRegistry._bind_arguments) and its digest BINDING
    # (strategy._HARNESS_BOUND_REVISIONS) covers `edit`/`rename`/`delete` but
    # NOT `write`. Setting this to True today therefore refuses every blind
    # `write` on the typed path, and the run mints `completed_verified` with
    # none of its edits applied. Measured on this tree, same suite, only this
    # value changed:
    #     False/None: 51/52 daily-driver arms ok, zero_false_verified_successes=True
    #     True:       41/52 arms ok,       zero_false_verified_successes=False
    # Ten arms went red (dd_21, dd_22, dd_24, dd_25, dd_26, both arms) and two
    # tests/test_agent_kernel.py tests with them. A default that causes a false
    # verified success is worse than a missing default, so the value stays None
    # until the binder covers `write`; the exact change and its verification
    # recipe are in harness/AGENTS.md under "Cross-terminal requests". A caller
    # that wants the strict reading on a path of its own sets it to True
    # deliberately; a caller that relaxes it to False gets
    # digest_relaxed=True + a reason on the edit receipt rather than silence.
    "require_edit_digest": None,
    "edit_post_check": True,  # run the syntax/validation gate after every edit
    # and roll the file back byte-for-byte when it fails
    "edit_ambiguity_candidates": 5,  # candidate locations listed on a refusal
    "edit_candidate_context_bytes": 120,  # context shown per candidate
    # Product-grade output (spec items 26/29)
    "git_output": True,  # on verified success: branch + commit + PR
    # description via execution.git_output (in the
    # harness's PRIVATE work copy — never the
    # original repo); failure degrades to a trace
    # event, never fails the verified fix
    "rationale_log": True,  # write logs/{task_id}/rationale.md (one
    # grounded paragraph, all outcomes) via
    # execution.rationale
    "branch_name": None,  # explicit branch name; None = harness/fix-<slug>
    # Reversible compaction — on-demand reinjection (spec item 13).
    # state.json is the compacted view; trace.jsonl keeps everything; a
    # step session's RECALL <terms> message pulls older detail back.
    "max_recalls_per_step": 3,  # RECALL budget per step session (a step
    # must still do its WORK in bash turns)
    "recall_results_cap": 5,  # max trace entries returned per RECALL
    "recall_max_chars": 4000,  # combined char cap on RECALL results
    # Lint gate (Round 8, Task C): fast host-side static analysis of the
    # agent's CHANGED files before a verify cycle. A syntax/undefined-
    # name failure short-circuits to a fix attempt (structured feedback)
    # instead of paying for a sandboxed pytest run to find the same
    # thing. Never gates success — verifier-gated completion stays
    # absolute; lint only short-circuits what it KNOWS is broken.
    "lint_gate": True,  # gate on/off
    "lint_names": True,  # undefined-name pass (false-negative
    # biased; pin off if a repo's style
    # defeats it)
    # Agent-written edge-case tests (Improvement Round 2, Tasks A+B):
    # after the final verify passes but BEFORE success is minted, the
    # harness has the model write additional tests probing edge cases
    # implied by the issue (boundary values, error conditions, obvious
    # adjacent cases). Every generated test goes through the SAME
    # verification pipeline as existing tests — a baseline run on a
    # pristine copy (rerun=0, exactly like the task baseline), then a
    # post-fix run with the same flake-rerun + full-suite regression
    # rigor as the final gate. A post-fix failure poisons the attempt
    # (the fix misses an issue-implied edge — retry with the failing
    # test as feedback); a GENERATION problem (unparseable reply, no
    # usable tests, model/verifier crash) SKIPS the gate — a verified
    # fix is never overturned over test-writing quality.
    "agent_tests": True,  # gate on/off
    "agent_tests_max": 3,  # max generated test files per attempt
    "agent_tests_dir": "tests/_agent_generated",  # reserved REPO-RELATIVE
    # dir (posix); harness-owned and
    # TRANSIENT — wiped before the diff,
    # never part of the delivered fix
    "agent_tests_max_chars": 12000,  # cap on TOTAL generated content
    # Self-critique before finalizing a fix (Agent Intelligence round).
    # After the final verify passes but BEFORE success is minted, one
    # extra model call reviews the diff against the ORIGINAL issue text
    # ("does this diff actually address what was reported, not just make
    # tests pass") — the failure class verification alone misses (a
    # technically-green diff that dodges the actual complaint). A "no"
    # verdict poisons the attempt (retry with the critique's reason as
    # feedback), same policy semantics as an edit-validation failure.
    "self_critique": True,  # False disables (the ablation's OFF arm)
    "self_critique_max_chars": 8000,  # diff cap fed to the critique call
    # Docs/API lookup (Round 8, Task D): a step session's DOCS <query>
    # message resolves library/API documentation (shared cache ->
    # interpreter docs via pydoc subprocess -> opt-in PyPI) and
    # re-injects it into the session. Read-only; the only remote call
    # is the fixed PyPI JSON endpoint, gated off by default.
    "docs_lookup_enabled": True,  # DOCS escape on/off
    "docs_lookup_allow_remote": False,  # PyPI fetch gate (network opt-in;
    # cache + pydoc are offline)
    "max_docs_per_step": 3,  # DOCS budget per step session
    "docs_max_chars": 3000,  # cap on one lookup's rendered result
    # Web-page reading (generalizes DOCS beyond installed libraries):
    # a step session's FETCH <url> message fetches ONE page (GET only),
    # extracts readable text (readability-style, boilerplate stripped),
    # and re-injects it into the session. Read-only by construction —
    # no forms, no auth, no arbitrary POST; host blocklist + scheme
    # allowlist + per-hop redirect validation guard the host-side fetch;
    # timeout/size/char caps bound both memory and context. Every fetch
    # is trace-logged (web_fetch event: URL + outcome + timestamp).
    "web_fetch_enabled": True,  # FETCH escape on/off (OFF arm)
    "max_fetches_per_step": 3,  # FETCH budget per step session
    "webfetch_timeout_s": 15,  # whole-fetch timeout (floor 3)
    "webfetch_max_bytes": 1_048_576,  # response cap, enforced DURING read
    "webfetch_max_chars": 3000,  # rendered-text cap fed to context
    "webfetch_max_redirects": 3,  # redirect hops, re-validated each hop
    # AGT-01: `web_fetch` and `web_search` are the retrieval capability, and
    # every strategy inherits it unless it explicitly withholds `network`.
    # Which HOSTS it may reach stays deny-by-default: the shipped allowlist is
    # unchanged, and an operator widens it with `webfetch_allowed_hosts`
    # (previously documented on harness.webfetch and read by nothing, so
    # retrieval was unreachable for every host that mattered). An explicit
    # empty list is honoured as deny-everything - the measurable OFF arm.
    "webfetch_allowed_hosts": None,
    "web_search_enabled": True,  # web_search on/off (OFF arm)
    "websearch_endpoint": "https://html.duckduckgo.com/html/",
    "websearch_timeout_s": 15,  # whole-search timeout (floor 3)
    "websearch_max_bytes": 1_048_576,  # response cap, enforced DURING read
    "websearch_max_chars": 3000,  # rendered-text cap fed to context
    "websearch_max_redirects": 2,  # redirect hops, re-validated each hop
    # AGT-01: a run-level narrowing ON TOP of whatever the strategy already
    # withheld. It can only SUBTRACT (a capability name, a tool name, or a
    # {name: reason} mapping); an entry naming nothing in the surface is
    # reported rather than ignored, so a typo cannot read as "nothing was
    # withheld". `None` is the "no opinion" reading and is behaviour-neutral.
    "capability_withheld": None,
    # Decision-memory-informed planning (Round 2, T1+T4): before the
    # planner call, query Terminal 4's decision store for past decisions
    # recorded against THIS repo (conventions, gotchas, architecture
    # choices) and inject them as a planner-prompt section. All knobs
    # here; decisions.db location is the memory module's own convention
    # (HARNESS_HOME / HARNESS_DECISIONS_DB env).
    "plan_with_memory": True,  # master switch (False = OFF arm of the
    # ablation, exactly one code path)
    "memory_query_limit": 6,  # max decisions injected into the prompt
    "memory_max_chars": 1500,  # combined char cap on the section
    # Coordinated multi-file changes (Improvement Round 2): when a fix
    # in one file structurally implies changes elsewhere (signature
    # change at every call site, rename across definition + callers),
    # the harness detects the fan-out via the structural graph and
    # treats the file set as ONE atomic unit — validated together and
    # rolled back together (editor.restore_group), never landed
    # partially.
    "coordination_detect": True,  # planning-side fan-out detection
    "coordination_min_files": 2,  # a "group" needs >= 2 files
    "coordination_gate": True,  # enforce group completeness pre-verify
    "coordination_rollback": "group",  # on a failed/partial group attempt:
    # "group" = roll back ONLY the group's
    # files (other steps' work survives),
    # "all" = full restore_dir (previous
    # behavior), "none" = leave work/ as-is
    # Skills system (Plugins round, Task A): auto-invoked markdown
    # instruction packs (SKILL.md folders under .neo/skills/ +
    # ~/.config/neo/skills/ + plugin bundles). Before planning, the
    # harness scans available skills' descriptions against the task and
    # injects the full SKILL.md of any that plausibly apply as a planner
    # prompt section. Same placement discipline as decision memory:
    # AFTER `## Retrieved context` so the difficulty predictor is safe.
    "skills_enabled": True,  # master switch (False = skip the scan)
    "skills_max": 3,  # max skills injected into one plan
    "skills_max_chars": 2500,  # combined char cap on the section
    "skills_roots": None,  # extra search roots (list of dirs; plugins
    # and tests pin explicit roots here)
    # Custom tools from plugins (Plugins round, Task C): plugin tool
    # definitions may extend the step loop's read-only command allowlist
    # (extra verbs allowed in BATCH entries) — see cli/plugins.py's
    # TOOL VERBS manifest key. "none" keeps the built-in allowlist.
    "plugin_tool_verbs": None,  # e.g. ["ruff", "mypy"] (or "none")
    # Mid-task steering (steering round): new user instructions can be
    # injected while a task runs (logs/{task_id}/steering.jsonl is the
    # transport); the loop consumes them at SAFE CHECKPOINTS — turn
    # boundaries within a step session, step boundaries, and the final
    # gate. Intents: guide (incorporate into the live session), replan
    # (abandon the remaining plan, re-plan with all steering visible,
    # keep work-in-progress), abort (clean stop, resumable). A pending
    # steering event BLOCKS success minting (verifier-gated completion
    # is never shortcut — the fix is re-verified after steering).
    "steering_enabled": True,  # False = the OFF arm (never polls)
    "max_pending_steering": 16,  # cap on the unconsumed queue (a
    # runaway injector can't grow the journal unboundedly)
    "steering_max_chars": 4000,  # cap on the re-plan context section
    # VEX-CEILING-07 — recovery policy knobs. The error-kind -> action table
    # itself lives in harness.tool_errors.POLICY (code, not config); these
    # are the BOUNDS, and they live here so a run is reproducible from its
    # merged config (spec item 10). No recovery constant is hardcoded in a
    # loop.
    # A hard steering abort must kill an in-flight child process within this
    # many seconds (the watcher polls at 50ms; the sandbox kills on the token,
    # so the process kill is what meets the bound).
    "abort_kill_deadline_s": 5,
    # Per-command budget escalation after a timeout. `command_timeout_s` is
    # the base; a timeout raises it by the backoff factor for the NEXT
    # command, hard-capped at recovery_max_timeout_s. The command is also
    # narrowed (fewer files / first failure only / bounded depth).
    "recovery_timeout_backoff": 1.5,
    "recovery_max_timeout_s": 600,
    # Doom-loop protection on the bash path: this many identical normalized
    # commands are allowed before the turn is STOPPED (the model is told to
    # change approach instead of burning the remaining turns). Read-only
    # commands are EXEMPT by default — a model re-reading a file after its own
    # edit is legitimate, and the typed tool catalog's own loop guard already
    # draws that line (see `loop_guard_read_only`). Set True to guard reads too.
    "recovery_max_repeat_command": 2,
    "recovery_loop_guard_read_only": False,
    # Evidence attached by the per-kind policy, all bounded reads.
    "recovery_listing_limit": 40,  # max entries in a file_not_found listing
    "recovery_numbered_file_lines": 160,  # max lines in a numbered file
    "recovery_numbered_file_chars": 6000,  # max chars in a numbered file
    "recovery_fallback_model": None,  # named fallback tier for model_unavailable
    # Bounded model-failure tolerance: a 429/5xx/connection/timeout is
    # retried with exponential backoff (base * 2^(n-1), capped) and emits
    # `model_recovery` per decision. Terminal failures (auth, bad request)
    # are NOT retried and are reported with their own kind — a transient
    # provider failure can no longer end a healthy run.
    "max_model_attempts": 3,
    "model_retry_base_s": 0.5,
    "model_retry_cap_s": 8.0,
    # AGT-02 — the BOUNDED REFLECTION LOOP. Every failure the loop sees
    # (a parse error, a lint finding, a failing test, a tool error, a blocked
    # command) becomes the NEXT user message verbatim, with its evidence
    # attached, so a run's struggle is recoverable instead of rediscoverable.
    # These two caps are what make that loop bounded and the behaviour
    # observable; both are reported in the run's `recovery_stats` receipt.
    #
    #   per_step  reflections spent since the last PRODUCTIVE turn (a
    #             successful tool call ends the step). This is the
    #             "three failures, then a fourth is refused" cap. A
    #             non-retryable failure (a harness/OS refusal, an environment
    #             fault, a terminal model failure) never charges either cap,
    #             so a refused call cannot be used to exhaust a run.
    #   per_run   reflections for the WHOLE run, so a long session cannot
    #             reflect forever. It is 4x the per-step cap: a run may
    #             recover from four separate failure streaks and a fifth is
    #             refused. Setting it equal to the step cap is a supported,
    #             measurable tighter arm.
    #
    # These ARE behaviour-changing defaults, deliberately: an unbounded
    # reflection loop is the defect this closes. They are in DEFAULTS (not
    # opt-in) because a cap that only exists when asked for is not a cap.
    "reflection_max_per_step": 3,
    "reflection_max_per_run": 12,
    # How much of a failure's text rides the reflection. Bounded with the
    # shared head+tail omission marker, so evidence is never silently cut.
    "reflection_evidence_max_chars": 4000,
    # Multi-mode routing (Modes round). The session classifies every
    # input into fix/build/question/research before any work starts
    # (harness.intent); gray-zone input uses ONE cheap model call
    # (difficulty_hint="easy" — adaptive routing picks the cheap tier;
    # intent_model pins a specific classifier model).
    "intent_enabled": True,  # False = legacy everything-is-a-fix behavior
    "intent_model": None,  # pinned classifier model; None = router choice
    # Question/Q&A mode (Task C): read-only, no sandbox, no verification.
    "qa_max_files": 4,  # retrieval files injected into the answer context
    "qa_context_lines": 80,  # lines per file in that injection
    "qa_max_reads": 6,  # READ <path> round-trips per question
    "qa_max_read_chars": 4000,  # cap per READ result
    # Research mode (Task E): read-only FETCH/DOCS-assisted synthesis.
    "research_max_fetches": 4,  # FETCH budget per research task
    "research_max_docs": 4,  # DOCS budget per research task
    "research_turns": 8,  # total model round-trips (hard bound)
    # Build/feature mode (Task D): acceptance-test authoring + the
    # unchanged fix pipeline. The tests are the completion contract —
    # they must FAIL on the pristine tree (baseline-confirmed) and the
    # full verification pipeline gates success against them.
    "build_tests_max": 3,  # max acceptance test files
    "build_tests_max_chars": 16000,  # cap on TOTAL authored content
    "build_tests_dir": "tests/_build_acceptance",  # reserved REPO-RELATIVE
    # dir inside the stage-1 base copy: the acceptance tests ride into
    # the fix loop's pristine/work trees as repo content (TDD shape —
    # the pristine-first git commit carries them; the delivered diff
    # shows the implementation; the regression gate covers them)
    # Long-horizon planning for build mode (multi-session projects): a
    # feature request too large for one session is decomposed into a
    # PROJECT PLAN of smaller, independently-checkpointed sub-tasks —
    # each itself an unchanged run_build — tracked ACROSS sessions by
    # logs/{project_id}/project.json (completed SUB-TASKS, not steps).
    # Acceptance criteria are extracted BEFORE decomposition and become
    # the whole-effort completion contract: every criterion id must be
    # covered by >=1 sub-task, and the final gate is criteria coverage
    # plus a full-suite verify on the accumulated tree.
    "build_project": False,  # router gate: large build requests dispatch
    # to harness.build_plan.run_project (multi-session) instead of
    # harness.build_mode.run_build (single-session)
    "project_max_sub_tasks": 4,  # cap on decomposed sub-tasks
    "project_sub_tasks_per_session": 1,  # session budget: sub-tasks
    # BUILT per run_project invocation before the checkpoint pause
    # (already-passing completions are free — they build nothing)
    "project_criteria_max": 8,  # cap on extracted acceptance criteria
    "project_resume": False,  # TRUE = continue an existing project
    # (by project_id) from its persisted plan
    # Scan mode (Proactive Health Scan round): `neo scan` analyzes a
    # repo WITHOUT a bug report (coverage gaps via the code graph,
    # latent-bug smells via AST, dependency pins) and ranks what is
    # genuinely worth attention. Read-only, no model calls by default;
    # the ONLY remote call is the opt-in PyPI freshness check (same
    # opt-in discipline as docs_lookup_allow_remote).
    "scan_max_findings": 8,  # findings shown in the report (the rest
    # stay in scan.json, resurfaced via --max-findings; the noise
    # budget is the point — 50 low-value findings beat nothing, but
    # 5 real ones beat 50)
    "scan_remote_deps": False,  # check pins against PyPI (network
    # opt-in; offline by default)
    "scan_pypi_timeout_s": 10,  # per-package PyPI JSON timeout
    "scan_smells_per_kind": 3,  # smell findings per kind per scan
    "scan_func_gap_max": 3,  # function-level coverage gaps per scan
    # General agent loop (interactive `neo` daily-use engine):
    # ONE tool-calling loop on the LIVE repo (not the pristine/work
    # benchmark path). No verifier gate unless tests are declared.
    # P1/W1-T1 (T1.W1.2). RAISED from 25 to 60.
    #
    # WHY 60, and what it costs. This is a per-TASK cap on tool-calling turns,
    # and each turn is at least one model call, so it is a COST decision before
    # it is a capability one. 25 was a number chosen when the daily path was a
    # demo; a real fix-plus-verify unit on a medium repository is routinely
    # 30-50 turns (read, run the test, read the traceback, edit, re-run, and
    # the loop repeats three or four times before it converges), so 25 truncated
    # ordinary work with no warning - the specific failure this phase exists to
    # remove. 60 is chosen rather than 50 because it is the smallest round
    # number above the observed p95 of real multi-attempt work, and because the
    # cost ceiling that actually protects a user is `budget_cap_usd` and
    # `max_wallclock_s`, both of which are enforced independently of this cap.
    # A turn cap that is the ONLY thing standing between a user and a runaway
    # bill is the wrong place to be generous; a turn cap that duplicates the
    # budget cap's job is the wrong place to be stingy. This one is the former
    # kind of safety net, not the latter, so it is set where a normal task
    # never notices it and a pathological one is still stopped.
    #
    # SPEND IMPLICATION, stated rather than assumed: at the default tier a turn
    # is one model call, so the worst case goes from 25 to 60 calls per task -
    # a 2.4x increase in the CEILING. The expected increase is far smaller
    # because most tasks finish well inside the cap and the cap only bounds the
    # tail; the measurable change is that runs which previously DIED at 25 now
    # finish. `turn_cap_approach_warn_turns` (below) makes the approach
    # visible so the tail is observable rather than silent.
    #
    # CALIBRATION WARNING - this value is a PROMPT INPUT, not only a bound.
    # `harness/prompts.py::CONTEXT_REINJECTION_TEMPLATE` renders
    # "Turn: {turn} of at most {max_turns}", and
    # `harness/agent_kernel/strategy.py::_reinjection` feeds this key into it.
    # Changing 25 -> 60 therefore CHANGES A PROMPT. AHEAD measured
    # system-prompt prose alone at -2.3pp, so `python -m evals.run` is the gate
    # for this line, not a comment. The four other read sites (all `int(...,
    # 25)` fallbacks) are enumerated in `harness/AGENTS.md`.
    "agent_max_turns": 60,  # tool-calling turns per agent task
    # P1/W1-T1: how close to `agent_max_turns` a run must be before it says so.
    # A cap that is only discovered by hitting it is a cliff, and a cliff is
    # worse than either a higher cap or a lower one. This is an OBSERVATION
    # threshold, not a new cap: the run does not stop, it announces.
    "turn_cap_approach_warn_turns": 10,
    "agent_approval": "auto",  # "auto" = run edits/bash + show diff;
    # "require" = BASH/EDIT/WRITE need approve_fn (else refused)
    "agent_context_files": 4,  # retrieval files in the opening context
    "agent_context_lines": 80,  # lines per file in that context
    "agent_max_read_chars": 12000,  # cap per READ result
    "agent_intent_enabled": True,  # False = everything is agent_task
    "agent_fetch_enabled": True,  # fetch tool on/off (False = honest skip)
    "agent_max_fetches": 4,  # FETCH budget per agent task (read-only web)
    "agent_live_bash": True,  # BASH runs live via local shell, never Docker
    # R2-15 (TRUST): the daily interactive path is SANDBOXED BY DEFAULT. This
    # is a deliberate, behaviour-changing default and it is the fix: the trust
    # asymmetry was that `neo fix` runs sandboxed behind a verifier gate while
    # the daily path defaulted to live host bash with no sandbox, so the path a
    # user trusts LEAST had the weakest boundary.
    #
    # It is read in exactly ONE place -- `harness/agent_kernel/strategy.py`'s
    # shell handler, which passes it to `SafeToolBackend.execute(...,
    # sandboxed=...)`, i.e. the Docker sandbox that refuses to fall back to the
    # host. MEASURED before it was flipped (this repository, `-p no:randomly`):
    # `tests/test_agent_kernel.py tests/test_ceiling_r2_04_daily_default.py
    # tests/test_tool_protocol.py tests/test_workspace_security.py` 148 passed
    # both before and after; `tests/test_cli_terminal_parity.py
    # tests/test_cli_command_system.py tests/test_ceiling14_resilience.py` 171
    # passed after; `python -m evals.run --check` 14/14 CLEAN after.
    #
    # OPERATOR OPT-OUT is `daily_sandbox = false` (see below), which
    # `shared.approval.resolve_daily_trust` turns into a loud, permanent receipt
    # naming the weaker boundary. Setting THIS key to false is also honoured as
    # an opt-out, and when the two disagree the receipt reports the WEAKER
    # boundary rather than the requested one.
    "agent_process_sandboxed": True,
    # R2-15: `daily_sandbox` is the operator-facing switch for the daily path's
    # containment. It is `None` here, NOT `True`, and that is load-bearing: a
    # value in DEFAULTS is merged into every task and every eval arm, so a
    # truthy default could not be told apart from a deliberate choice. Absent /
    # `None` / `True` all mean "sandboxed" (the safe default); an explicit
    # `False` is an opt-out and is always reported. An unparseable value is
    # treated as "no opinion" and can never disable the boundary.
    "daily_sandbox": None,
    # R2-15: trust calibration. `None` = no opinion, and the module's default is
    # ON (an approval the operator already gave is remembered for the session so
    # they are not asked the same question forever). `False` turns it off and
    # every action is asked again. Cross-session PERSISTENCE is a separate,
    # default-off key: a grant that outlives the process is a grant nobody
    # re-confirmed, so it is opt-in.
    "approval_calibration": None,
    "approval_calibration_persist": None,
    "approval_calibration_max_grants": 200,  # pure bound on the grant journal
    # The widest approval scope the daily path will retain. `global` is NOT a
    # scope and is not listed: it matched every call regardless of tool, effect,
    # repository, or command, so one answer defeated the whole policy (R2-15).
    "approval_scope": "session_command_prefix",
    "agent_mcp_servers": {},  # extra label->launch-cmd MCP servers (dict)
    "agent_kernel_enabled": False,
    "agent_kernel_compatibility": True,
    # Strategy is the kernel's single authority. None means "the caller named
    # no strategy", which resolves to `daily` (the resolver's own default) --
    # `harness.agent_loop.resolve_agent_dispatch` is the ONE place that
    # decision is made. A name here (daily, verified_fix, planning, question,
    # research, legacy_agent) is HONORED, and an unknown name is a hard error
    # rather than a silent fallback.
    "agent_strategy": None,
    # COMPATIBILITY DEFAULT (behaviour-changing opt-in; see CHANGELOG).
    # Absent/None/""/"none"/"default" == "no override", so the None default
    # is behaviour-neutral. Set it to "legacy_agent" to restore the
    # pre-0.3.0 default engine for an existing user or a pinned integration.
    # Only a PRESENT, non-empty, resolvable name changes behaviour, so this
    # key never silently switches a run the way a value in DEFAULTS would.
    "agent_default_strategy": None,
    "safe_tool_backend": True,
    "permission_rules": [],
    "agent_permission_rules": [],
    "permission_default": None,
    # R2-15: `global` is DELETED from the scope table, not renamed. A scope that
    # matches every call is an unconditional bypass, so the supported set is
    # once / exact_call / session_path / session_command_prefix. A caller that
    # asks for `global` is narrowed to `once` and the reason is recorded
    # (`shared.approval.retired_scope_note`), not silently substituted.
    "approval_scopes": ["once", "exact_call", "session_path", "session_command_prefix"],
    "session_max_turns": 24,
    # P1/W1-T1 (T1.W1.2) - the REAL conversation cap, and the honest naming.
    #
    # READ `session_max_turns` ABOVE FIRST: it does NOT bound the conversation.
    # Its one reader is `harness/agent_kernel/kernel.py`, which passes it to
    # `ContextBuilder(max_turns=...)`, and that is a bound on how many prior
    # CONTEXT-SUMMARY records the compiler keeps. The full derivation and the
    # reasoning are in `harness/turn_caps.py`, which is now the one authority
    # for both caps.
    #
    # This key is the actual conversation-length cap, and it is `None` (i.e.
    # UNBOUNDED) rather than a number for two reasons, both load-bearing:
    #   1. It is the honest statement of today's behaviour. Inventing a default
    #      here would make the receipt claim a limit the product does not have.
    #   2. A real value in DEFAULTS is merged into EVERY task and EVERY eval
    #      arm, so shipping one would silently truncate conversations across
    #      the whole project on the day the key landed. Setting it is an
    #      operator's decision, made per session, with the spend in mind.
    "session_max_conversation_turns": None,
    "session_summary_max_chars": 6000,
    "session_diff_max_chars": 12000,
    # Rolling conversation budget. Prior assistant/tool turns are retained
    # inside this budget; turns dropped past it become a structured handoff.
    "agent_conversation_messages": 48,
    "agent_conversation_chars": 24000,
    "agent_conversation_tool_chars": 4000,
    "agent_conversation_handoff_chars": 4000,
    # Context budget, measured in prompt TOKENS rather than characters.
    # Compaction fires at `context_compaction_fraction` of the usable window so
    # the provider is never handed an over-window request. The window itself has
    # ONE authority - `runtime.model_capabilities.resolve_context_window` (probe
    # -> litellm model info -> local table -> documented floor) - which
    # `harness.agent_kernel.budget.budget_from_config` READS and applies as a
    # CEILING. So `context_window_by_model` and `context_window_tokens` may lower
    # a run's window but can never raise it past what the model really has, and
    # an unknown model keeps whatever the run declared (the authority's floor is
    # a refusal to guess, not a capability answer). The budget's
    # `window_source` says which rung produced the number.
    # `context_token_estimator` is heuristic by default (deterministic,
    # offline, and biased to round up so a run compacts early).
    "context_budget_enabled": True,
    "context_window_tokens": 32768,
    "context_window_by_model": {},
    "context_reserved_output_tokens": 4096,
    "context_compaction_fraction": 0.6,
    "context_token_estimator": "heuristic",  # heuristic | auto | tiktoken
    "context_category_shares": {},
    "context_compaction_model": "",  # "" = the run's own model
    "context_compaction_fallback_model": "",  # cheaper model on summary failure
    "context_compaction_input_tokens": 6000,
    # Which tier does the SUMMARISER run on? ``cheap`` (the default) prefers
    # the run's cheap/easy tier for compaction, because summarisation is the
    # most mechanical job in the loop and AGT-08 measured no reason to pay a
    # frontier price for it; ``expensive`` is the explicit opt-in back to the
    # run's own model. ``context_compaction_model`` still wins outright when
    # it names a model, and either way the `context_compacted` receipt names
    # the tier that ran, the model that summarized, and whether the
    # configured tier was the one that ran.
    "context_compaction_tier": "cheap",  # cheap | expensive | run
    # Effort ladder (AGT-08). ``auto`` sends NO effort parameter, so this
    # default is byte-identical to the pre-AGT-08 request on every task and
    # every eval arm - which is the only reason it is safe to publish here
    # (a DEFAULTS value is merged into all of them). `NEO_EFFORT` and
    # `Task.config["effort"]` and a mid-run `/effort` all set the same key;
    # `runtime.model_capabilities.map_effort` is the ONE authority that turns
    # a level into a provider parameter, and it reports `unsupported_*` rather
    # than pretending. `effort_parameter` overrides the knob choice: a string
    # names the parameter to send, `None`/"none"/"off" sends nothing at all.
    "effort": "auto",  # auto | low | medium | high | xhigh | max
    "effort_parameter": None,
    # Compaction that cannot do its job must FAIL, not repeat. A compaction whose
    # purpose is to bring the request back under `context_compaction_fraction`
    # and which leaves it at or above that trigger will be asked again on the
    # next turn with the same inputs and the same answer; three of those in a row
    # means the protected prefix plus the handoff alone exceed the trigger, which
    # is a task/budget misconfiguration rather than a strategy. The run then ends
    # `failed` with the reason (Claude Code bails on the same shape). The streak
    # resets on the first compaction that DOES get back under the trigger, so a
    # healthy long session is never accused of thrashing. `0` disables the guard.
    "compaction_thrash_limit": 3,
    # Floor used only when a caller measures a compaction with no trigger in the
    # receipt (an unbounded probe rather than a real run). A real compaction is
    # judged on whether it got back under its trigger, not on a token delta.
    "compaction_min_reclaim_tokens": 1,
    # Prefix-stable step prompt for the LEGACY step loop
    # (`harness.core.run_step`). Truthy switches the session from the historical
    # single interpolated system message to
    # `harness.prompts.render_step_messages`'s
    # `[system(frozen), system(per-turn), user]` shape, so consecutive turns
    # share a byte-identical leading segment.
    #
    # It is `None` here, NOT `False`, on purpose. A value in DEFAULTS is merged
    # into every task, so defaulting this to True would silently change every
    # run AND every eval arm, and it would change the message shape a dozen
    # scripted models dispatch on (`next(m for m in messages if m["role"] ==
    # "system")`) - a change that reads as a prompt regression rather than as a
    # cache optimization. Absent / None / False all mean the historical shape, so
    # enabling this is an explicit, single-key decision.
    #
    # Before enabling it, migrate any scripted model that reads the step system
    # message to `harness.prompts.step_system_text(messages)`, which is correct
    # for BOTH shapes. See `harness/AGENTS.md` for the measured list.
    "step_prompt_cache_split": None,
    # Constraint re-injection rides the END of a long context.
    "context_reinjection_enabled": True,
    "context_reinjection_fraction": 0.35,
    # Per-turn file pre-images that make a files-only rewind exact.
    "context_rewind_max_files": 200,
    "max_tool_recoveries": 3,
    "max_model_failures": 2,
    # Tool fan-out: read-only tools in one turn run concurrently, everything
    # else sequentially. The concurrency CLASS comes from each tool's catalog
    # entry (`read_only`); these keys bound the fan-out itself.
    "max_parallel_tools": 4,
    "max_tool_fanout": 8,
    "max_tool_fanout_chars": 24000,
    # Tool output is the dominant context cost, so it is capped at STORAGE time
    # (before the conversation, the journal, the trace row and the next request
    # see it), in tokens rather than characters. 0 disables the cap.
    "tool_output_token_limit": 4000,
    # Doom loop: the same canonical tool call with identical arguments this many
    # times stops the run and asks the user, instead of being silently refused
    # and re-emitted. Read-only tools stay exempt (a re-read is legitimate
    # exploration); set `loop_guard_read_only` to arm them too.
    "max_repeat_tool_calls": 3,
    "loop_guard_read_only": False,
    # Bounded search: above this many matches a search ERRORS with a sample
    # rather than returning a result set. There is deliberately no paging
    # affordance — see harness/retrieval.py::PAGING_ARGUMENTS.
    "search_max_matches": 50,
    "search_sample_matches": 10,
    "search_max_files_scanned": 5000,
    # ------------------------------------------------------------------
    # Compiled context, symbols, language-server feedback, memory capture.
    # `knowledge_enabled` is the single OFF arm: False means the compiler is
    # never constructed and no compiled block reaches any model request, so an
    # ablation differs from the ON arm by exactly this key.
    "knowledge_enabled": True,
    "knowledge_block_max_chars": 24000,
    "knowledge_tool_max_chars": 6000,
    "knowledge_diagnostics_max_chars": 2000,
    "context_token_budget": 12000,
    "context_chars_per_token": 4,
    "context_cache_entries": 32,
    "context_recent_turns": 6,
    "context_map_symbols": 25,
    "context_dependency_limit": 20,
    "decision_memory_enabled": True,
    # Language-server feedback. Off by default because it starts a real
    # process; when on, a missing or slow server degrades to "no diagnostics"
    # and never fails a run.
    "lsp_enabled": False,
    "lsp_timeout_s": 5.0,
    # Durable memory capture from the agent. `memory_record` is additionally
    # permission-gated in the canonical tool catalog, so approval is required
    # before this key is even consulted.
    "memory_record_enabled": True,
    # ------------------------------------------------------------------
    # AGT-07 -- two axes and the approver agent.
    #
    # Every key here is `None`, which is BEHAVIOUR-NEUTRAL in this module's
    # own reading (a `None` is what an absent key already means to every
    # consumer) and deliberate as an opt-in switch: an approver model call on
    # every privileged action, and a declared writable-root set, are changes
    # an operator asks for, not changes a merged default should make for
    # every task and every eval arm.
    #
    # `approver_agent` is the master switch and is read by KEY PRESENCE plus
    # truthiness (`harness.approver.ApproverAgent.from_config`). Absent /
    # None / False = no approver model in this run, which is the historical
    # behaviour. `harness/approver.py` is fail-closed by construction and
    # there is deliberately no key that turns THAT off: an approver that
    # fails open is not an approver, and an opt-out from fail-closed would be
    # an opt-out from the approval itself.
    "approver_agent": None,
    "approver_model": None,  # None = the router's cheap tier, not a fixed name
    "approver_timeout_s": None,  # None -> harness.approver.DEFAULT_TIMEOUT_S
    "approver_max_chars": None,  # None -> harness.approver.DEFAULT_MAX_CHARS
    # Axis (a) -- technical containment. `sandbox_readonly_paths=None` keeps
    # execution.sandbox.DEFAULT_READONLY_SUBPATHS (`.git` and friends mounted
    # read-only INSIDE the writable root); an explicit `[]` genuinely disables
    # them, which is why None and [] are different values here.
    # `sandbox_writable_roots=None` leaves the historical whole-workspace
    # bind mount; a non-empty list makes the workspace mount read-only and
    # mounts only those subtrees read-write.
    "sandbox_readonly_paths": None,
    "sandbox_writable_roots": None,
    # --- AGT-05 plan-phase research isolation ----------------------------
    #
    # `plan_research` is the ONLY master switch, and it is `None` here rather
    # than True. A value in DEFAULTS is merged into every task and every eval
    # arm, so a truthy default would silently switch every run in the project
    # onto a different architecture (an extra bounded model call before turn
    # 1). The plan subagent is a real cost, so it is opted into. Absent,
    # None, and False all mean "no plan subagent" and the run's prompt is
    # byte-identical to what it was before this key existed - which is what
    # keeps the prompt-regression matrix comparable.
    #
    #   plan_research                 master switch; read by key presence AND
    #                                 truthiness, and a string must be an
    #                                 explicit yes ("1"/"true"/"yes"/"on"/
    #                                 "research") so a typo cannot enable it
    #   plan_model                    the architect model (Aider's
    #                                 architect/coder split). None = the run's
    #                                 own model plans. A second gateway bound
    #                                 to it, reusing the run's own boundary; if
    #                                 that boundary is unreachable the key is
    #                                 NOT honoured and the receipt says which
    #                                 model actually ran.
    #   plan_research_max_turns       bound on the researcher's own turns
    #   plan_research_max_tool_calls  bound on the researcher's tool calls
    #   plan_research_max_cost_usd    bound on the researcher's model spend
    #   plan_research_max_chars       cap on the RETURNED plan (0 = return
    #                                 nothing, which is a real choice, not an
    #                                 accidental "return everything")
    #   plan_research_max_citations   cap on the returned citations
    #
    # All six bounds are `None` here, and each has a bounded internal default
    # in `harness.agent_kernel.subagents`. None is behaviour-neutral because
    # an absent key already means "use the internal default"; publishing a
    # real value would change every run at once. Each is clamped to a floor of
    # zero, so a config that names nonsense degrades to the default rather than
    # to an unbounded researcher.
    "plan_research": None,
    "plan_model": None,
    "plan_research_max_turns": None,
    "plan_research_max_tool_calls": None,
    "plan_research_max_cost_usd": None,
    "plan_research_max_chars": None,
    "plan_research_max_citations": None,
}


def get_config(task_config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a config dict merging DEFAULTS with task.config overrides.

    Assumes task_config is a plain dict (Task.config); unknown keys are
    passed through untouched so other terminals can extend without this
    module needing changes. Values from task.config always win.

    One env-aware key. ``effort`` is read from ``Task.config`` first, then
    ``NEO_EFFORT``, then the ``auto`` default. The check is on the CALLER'S
    dict, not the merged one, because ``DEFAULTS["effort"] = "auto"`` is always
    present after the merge and would otherwise make the environment
    unreachable. The value is passed through verbatim: an unrecognised level
    is REPORTED as `invalid` by the router rather than being rounded here,
    so a typo is visible instead of silently becoming the default.
    """
    caller: Mapping[str, Any] = task_config or {}
    merged: Dict[str, Any] = dict(DEFAULTS)
    merged.update(caller)
    if not str(caller.get("effort") or "").strip():
        from_env = str(os.environ.get(EFFORT_ENV_VAR) or "").strip()
        if from_env:
            merged["effort"] = from_env
    return merged
