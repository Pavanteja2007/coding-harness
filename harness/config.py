"""Config handling for the harness.

All tunables come from task.config (project convention: no hardcoded
constants), with these defaults applied for anything missing. The defaults
match the project spec's Phase 1 guidance (~3 retries, cheap default model).
"""

from typing import Any, Dict

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
    # Harness behavior
    "max_step_turns": 15,  # bash-command turns per planner sub-step
    "max_output_chars": 3000,  # cap on tool output fed back to the model
    "context_files_cap": 4,  # files injected into a step session's context
    "context_lines_cap": 60,  # lines per file in that injection
    "max_file_bytes": 200_000,  # refuse to read/edit files larger than this
    "test_command": None,  # e.g. "python -m pytest -x"; None = autodetect
    "target_test": None,  # e.g. "tests/test_mathutil.py::test_mean"
    "baseline_reruns": 1,  # reruns for flake detection in verify()
    "protected_paths": [],  # globs the agent must not modify, e.g. tests/
    "work_subdir": "logs",  # where logs/{task_id}/ lives (repo-relative)
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
    # instruction packs (SKILL.md folders under .vex/skills/ +
    # ~/.config/vex/skills/ + plugin bundles). Before planning, the
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
    # Scan mode (Proactive Health Scan round): `vex scan` analyzes a
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
    # General agent loop (interactive `vex` daily-use engine):
    # ONE tool-calling loop on the LIVE repo (not the pristine/work
    # benchmark path). No verifier gate unless tests are declared.
    "agent_max_turns": 25,  # tool-calling turns per agent task
    "agent_approval": "auto",  # "auto" = run edits/bash + show diff;
    # "require" = BASH/EDIT/WRITE need approve_fn (else refused)
    "agent_context_files": 4,  # retrieval files in the opening context
    "agent_context_lines": 80,  # lines per file in that context
    "agent_max_read_chars": 12000,  # cap per READ result
    "agent_intent_enabled": True,  # False = everything is agent_task
    "agent_fetch_enabled": True,  # fetch tool on/off (False = honest skip)
    "agent_max_fetches": 4,  # FETCH budget per agent task (read-only web)
    "agent_live_bash": True,  # BASH runs live via local shell, never Docker
    "agent_mcp_servers": {},  # extra label->launch-cmd MCP servers (dict)
}


def get_config(task_config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a config dict merging DEFAULTS with task.config overrides.

    Assumes task_config is a plain dict (Task.config); unknown keys are
    passed through untouched so other terminals can extend without this
    module needing changes. Values from task.config always win.
    """
    merged: Dict[str, Any] = dict(DEFAULTS)
    merged.update(task_config or {})
    return merged
