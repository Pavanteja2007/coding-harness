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
