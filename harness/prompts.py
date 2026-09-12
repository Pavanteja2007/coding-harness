"""Prompts for the harness: planner, step-session system + observation,
submit/exit, and constraint re-injection blocks.

Design notes (spec items 14/15/16):
- Task decomposition: the planner breaks the issue into 2-4 small sub-steps,
  EACH with a pass/fail checkpoint, instead of one-shotting the whole fix.
- Constraint re-injection: critical constraints are restated at the END of
  the latest tool output (max recency) on every turn — compliance decays
  with distance from the instructions.
- Per-step context: only the current step's relevant files are injected
  (curated per-step, not a static growing bundle).
- Verifier-gated completion: SUBMIT ends a step session, but success is
  only ever decided by the verifier in core.py, never by the model's claim.
- Agent-written edge-case tests (Improvement Round 2): before success is
  minted, one generation call produces edge-case tests probing the issue's
  implied boundaries; they run through the SAME verify() pipeline (Task B)
  — never a separate, lighter path.
"""

from typing import Dict, List, Optional

PLANNER_SYSTEM = """\
You are a meticulous senior software engineer planning a bug fix.

You will be given an issue report and a partial view of the repository.
Your job is to break the fix into a SMALL number of incremental sub-steps
(2-4), each independently verifiable, rather than one giant change.

Each sub-step must have a concrete pass/fail checkpoint (a specific test,
command, or observable behavior). The agent executing your plan works in
a COPY of the repo, edits files via bash commands, and verifies with the
repo's test suite.

Output STRICTLY this JSON object and nothing else:
{
  "analysis": "1-3 sentences: what's wrong and where",
  "plan": [
    {
      "id": 1,
      "description": "short imperative description of the sub-step",
      "checkpoint": "how to check THIS step worked (test/command/behavior)",
      "files_hint": ["file1.py", "file2.py"],
      "change_group": "optional-name"
    }
  ]
}

Rules:
- No more than 4 sub-steps; prefer fewer for small bugs.
- Do not include a step "run the full test suite" — the harness verifies
  automatically after every step.
- files_hint: repo-relative paths you believe must change or be read.
- change_group: ONLY when this step's file edits are one part of a
  coordinated multi-file change (a signature change rippling to call
  sites, a rename across definition and callers, a field rename across
  model and serializers). Every step editing files in the SAME atomic
  change uses the SAME change_group name. The harness validates the
  group's files as ONE unit and rolls them back together on failure —
  a partial coordinated change never lands.
{coordination_rules}"""

# Where {coordination_rules} lands in PLANNER_SYSTEM (appended after the
# base rules; replaced literally — the template's JSON example braces
# must not go through str.format).
_COORDINATION_RULES_SLOT = "{coordination_rules}"

# Extra planner rules rendered ONLY when the structural fan-out detected
# a coordinated change (Improvement Round 2, Task A).
PLANNER_COORDINATION_RULES = """\
- This task IS a coordinated multi-file change: the plan must cover EVERY
  file in the group (do not defer call-site updates to a later task) and
  each step editing group files carries the same change_group name."""

PLANNER_USER = """\
## Issue
{issue_text}

## Retrieved context ({strategy} — may be incomplete)
{context_block}

## Coordinated-change fan-out (structural graph)
{coordination_block}

## Relevant past decisions (from earlier tasks in this repo)
{memory_block}

## Constraints
{constraints_block}

Produce the plan JSON now."""


STEP_SYSTEM_TEMPLATE = """\
You are an expert software engineer fixing ONE sub-step of a bug fix in a
Python repository. You interact ONLY through bash commands, one per turn.

## Environment
- Working dir: the repo root (you are in a copy — edits are expected).
- OS: POSIX-style bash. Windows note: paths use forward slashes; python \
is on PATH.
- Output limit: outputs longer than ~{max_output_chars} chars are truncated.

## Overall issue
{issue_text}

## Plan for this task (your step is #{step_id} of {total_steps})
{plan_block}

## Steps already completed (do NOT redo them)
{completed_block}

## Files relevant to this step (pre-loaded for you)
{context_block}

## How to finish
- When this step is done and its checkpoint passes, output exactly:
SUBMIT
- If you believe the step is IMPOSSIBLE, output exactly:
ABORT <one-line reason>

## Recovering compacted-away context (RECALL)
Sessions are fresh per step, so earlier detail (previous steps' commands,
outputs, verifier tails) is NOT in your context by default. If you find you
need something mentioned earlier in this task, output:
RECALL <search terms>
instead of a bash command. The harness searches this task's full trace and
replies with the matching entries; the RECALL line itself is never executed.
Use it sparingly (e.g. "RECALL pytest failures step 1") — usually re-running
a command is cheaper than recalling.

## Batched read-only commands (BATCH)
When a step needs several INDEPENDENT read-only operations (reading
multiple files, several unrelated searches), you may run them together
in ONE turn:
BATCH cat src/a.py ;;; cat src/b.py ;;; grep -n TODO src/
Each entry must be a SIMPLE read-only command (cat/head/tail/ls/dir/
find/grep/rg/wc/file/stat/pwd/which/where/env/git status|diff|log|show|
blame|ls-files/python -m pydoc) with NO pipes, redirects, ;, &&, or
backticks — a non-conforming entry rejects the whole batch. Entries run
concurrently; results come back labeled. Order-independent: do not rely
on one entry's output to write another.

## Library/API documentation lookup (DOCS)
If you need documentation for a library or API not obvious from the
repo (a module's functions, a symbol's signature), output:
DOCS <dotted target> [optional topic words]
instead of a bash command. The harness looks it up (local docs cache,
then the installed interpreter's documentation; PyPI metadata only if
enabled for the task) and replies with the rendered docs. The DOCS line
itself is never executed. e.g. "DOCS json.dumps indent options".

## Reading a web page (FETCH)
When the answer is on a web page neither the repo nor the interpreter
carries (an unfamiliar library's usage docs, a stdlib HOWTO, an error's
explanation), output:
FETCH <full url including http:// or https://>
instead of a bash command. The harness fetches the page (read-only GET,
no forms or auth), extracts the readable text (navigation and ads are
stripped), and replies with it. The FETCH line itself is never executed.
Use it for documentation pages — e.g. "FETCH https://pypi.org/project/\
num2words/". A handful of fetches per step; the reply is capped, so
prefer the most specific page.

## Step rules
- One bash command per turn (you may compose with &&).
- Do not modify test files unless the issue explicitly says the TEST is \
wrong; fix the CODE, not the tests.
- Keep edits minimal and focused on this step only.
- If this step belongs to a `change_group`: your edits are one part of an \
ATOMIC multi-file change. Either every file in the group changes across \
the group's steps, or the whole group is rolled back together — never \
leave a call site half-updated.
- Never claim the overall task is done — the harness verifies.
"""

# Appended to the END of every tool result (constraint re-injection —
# spec item 15: compliance decays with distance; keep these close to the
# most recent context).
CONSTRAINT_REINJECTION_TEMPLATE = """\

## REMINDER — CRITICAL CONSTRAINTS (re-stated)
- Overall issue: {issue_one_line}
- Current step ({step_id}/{total_steps}): {step_desc}
- Remaining steps: {remaining_steps}
- Files already completed: {completed_one_line}
- Do NOT touch: {protected_paths}
- Do NOT modify tests unless the issue explicitly says the test is wrong.
- When this step's checkpoint passes, output SUBMIT on its own line."""


SELF_CRITIQUE_SYSTEM = """\
You are a skeptical code reviewer checking a completed bug-fix attempt.
You are given the ORIGINAL issue report and the FULL diff of the proposed
fix. Your job is to answer ONE question honestly: does this diff actually
address what was reported, or does it merely make the tests pass?

Failure modes you are looking for:
- Fixing the SYMPTOM the tests check while leaving the reported cause
  (e.g. the issue says a line is dropped, the diff special-cases the test
  input instead of fixing the boundary condition).
- Defusing or weakening the test instead of fixing the code.
- Fixing something ADJACENT to the report (a real bug, but not THE bug).
- Overfitting to the exact test values instead of the general behavior.
- Cosmetic/doc-only changes over a behavioral complaint.

Output STRICTLY this JSON object and nothing else:
{
  "addresses_issue": true | false,
  "reason": "1-2 sentences citing the specific diff hunk and the specific
             complaint it does or does not address"
}

Be strict but fair: a small, minimal fix that genuinely repairs the
reported behavior is a YES even if imperfect. Answer NO only when the
diff fails to address the actual complaint.
"""


def render_self_critique_prompt(
    issue_text: str,
    diff: str,
    verification_summary: Optional[str] = None,
    feedback_objects: Optional[List[Dict]] = None,
) -> List[Dict[str, str]]:
    """Messages list for the self-critique review call (config-gated step
    in core.run_task, before a verified success is finalized).

    Assumes issue_text is the original report and diff is the full unified
    diff of the proposed fix ("" is allowed — an empty diff on a passing
    verify is itself critique-worthy). verification_summary and
    feedback_objects (INTERFACES.md Boundary 7 structured_feedback dicts)
    are optional context: what the verifier saw on the run's FAILING
    attempts tells the critique which complaint it must not dodge.
    """
    parts = [f"## Original issue\n{issue_text or '(none given)'}\n"]
    if verification_summary:
        parts.append(f"## Verification summary\n{verification_summary}\n")
    if feedback_objects:
        rendered = []
        for fb in feedback_objects[:5]:
            line = str(fb.get("summary") or "(unparseable failure)")
            if fb.get("test_id"):
                line = f"{fb['test_id']}: {line}"
            rendered.append(f"- {line}")
        parts.append(
            "## What the verifier reported on failing attempt(s) of this task\n"
            + "\n".join(rendered)
            + "\n"
        )
    parts.append(
        f"## Proposed fix (full diff)\n```diff\n{diff or '(empty diff)'}\n```\n"
    )
    parts.append(
        "Does the diff address the original issue? Output the JSON verdict now."
    )
    return [
        {"role": "system", "content": SELF_CRITIQUE_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


def issue_one_line(issue_text: str, width: int = 200) -> str:
    """First line-ish compression of the issue for re-injection blocks."""
    flat = " ".join((issue_text or "").split())
    return flat[: width - 1] + "…" if len(flat) > width else flat


def render_planner_prompt(
    issue_text: str,
    context_block: str,
    constraints_block: str,
    strategy: str = "grep",
    memory_block: Optional[str] = None,
    coordination_block: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Messages list for the planner call. Assumes context_block and
    constraints_block are pre-rendered strings; `strategy` describes how
    the context was retrieved (shown to the model so it can weigh the
    context's reliability); `memory_block` is the rendered decisions-from-
    memory section (None/empty → "(none)" placeholder); `coordination_block`
    is harness.coordination.format_coordination_block output (None/empty →
    "(none detected)"). The memory section
    deliberately sits AFTER `## Retrieved context`: Terminal 3's difficulty
    predictor cuts the first user message at that marker, so decision
    memory must not shift difficulty scoring. The coordination section
    sits between them for the same reason (it must be IN the prompt but
    after the cut, so it informs planning without inflating the
    difficulty signal from the issue itself)."""
    detected = bool(coordination_block and coordination_block.strip())
    system = PLANNER_SYSTEM.replace(
        _COORDINATION_RULES_SLOT,
        PLANNER_COORDINATION_RULES if detected else "",
    )
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": PLANNER_USER.format(
                issue_text=issue_text or "(none given)",
                context_block=context_block or "(none)",
                constraints_block=constraints_block or "(none)",
                strategy=strategy or "grep",
                memory_block=memory_block or "(none recorded yet)",
                coordination_block=(
                    coordination_block.strip() if detected else "(none detected)"
                ),
            ),
        },
    ]


def render_step_system(
    issue_text: str,
    plan: List[Dict],
    step_id: int,
    total_steps: int,
    completed_block: str,
    context_block: str,
    max_output_chars: int,
) -> str:
    """System prompt for one step session (deliberate context reset per
    step — spec items 12/13/16)."""
    plan_lines = []
    for st in plan:
        marker = " <- CURRENT" if st.get("id") == step_id else ""
        plan_lines.append(
            f"{st.get('id')}. {st.get('description')} "
            f"[checkpoint: {st.get('checkpoint')}]{marker}"
        )
    return STEP_SYSTEM_TEMPLATE.format(
        issue_text=issue_text or "(none given)",
        plan_block="\n".join(plan_lines) or "(empty plan)",
        step_id=step_id,
        total_steps=total_steps,
        completed_block=completed_block or "(none yet)",
        context_block=context_block or "(none)",
        max_output_chars=max_output_chars,
    )


def render_constraint_reinjection(
    issue_text: str,
    plan: List[Dict],
    step_id: int,
    total_steps: int,
    completed: List[str],
    protected_paths: List[str],
) -> str:
    """The re-injection block appended to the end of every tool result."""
    remaining = [
        f"{st.get('id')}. {st.get('description')}"
        for st in plan
        if int(st.get("id", 0)) > step_id
    ]
    step = next((st for st in plan if int(st.get("id", 0)) == step_id), {})
    return CONSTRAINT_REINJECTION_TEMPLATE.format(
        issue_one_line=issue_one_line(issue_text),
        step_id=step_id,
        total_steps=total_steps,
        step_desc=step.get("description", ""),
        remaining_steps="; ".join(remaining) or "(this is the last step)",
        completed_one_line="; ".join(completed) or "(none)",
        protected_paths=", ".join(protected_paths) or "(none specified)",
    )


def render_first_user(context_block: str) -> str:
    """First user message of a step session (after system prompt)."""
    return (
        f"{context_block or '(no extra context)'}\n\n"
        "Begin. Respond with your first bash command."
    )


def render_recall_result(query: str, entries: List[Dict]) -> str:
    """The user message returned to a session that issued `RECALL <query>`.

    entries is TraceLogger.find_events output: [{"line": int, "kind": str,
    "data": <json dump>}]. Assumes the data strings are already
    length-capped; this renderer adds only a compact header per entry.
    """
    if not entries:
        return (
            f"No earlier trace entries match '{query}'. The compacted "
            "detail you're looking for may not exist — proceed with bash "
            "commands, or SUBMIT if the step is done."
        )
    lines = [f"RECALL results for '{query}' (most recent last):"]
    for e in entries:
        lines.append(f"--- trace line {e['line']} [{e['kind']}] ---")
        lines.append(str(e["data"]))
    lines.append(
        "End of RECALL results. Continue with exactly ONE bash command, "
        "or SUBMIT if this step is done."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent-written edge-case tests (Improvement Round 2, Task A)
# ---------------------------------------------------------------------------

AGENT_TESTS_SYSTEM = """\
You are a meticulous senior engineer writing EDGE-CASE tests before a fix
is accepted.

You will be given an issue report and the proposed fix (unified diff).
The fix already makes the reported failing test pass. Your job is the
check a careful human would do next: probe the cases the issue IMPLIES
but does not spell out — boundary values, error conditions, and obvious
adjacent cases the fix could plausibly still get wrong.

Write ONE pytest test FILE per edge-case cluster. Rules:
- Test the PUBLIC behavior described in the issue, not implementation
  details. Import the package the repo's own tests import (absolute
  imports like the repo uses, e.g. `from numlib.mathutil import mean`).
- Each test must be deterministic and self-contained (no network, no
  files outside the repo, no randomness without a seed, no time-of-day).
- Only write tests you expect a CORRECT fix to PASS. You are probing
  for incompleteness in the proposed fix, not authoring the project's
  whole future test suite. Aim for {max_tests} file(s) or fewer; each
  may hold several related assertions.
- Do NOT rewrite or duplicate the reported failing case.
- If the issue gives you nothing to probe beyond the given test, return
  an empty list — an empty answer is honest and fine.

Output STRICTLY this JSON object and nothing else (no code fences):
{{
  "tests": [
    {{"filename": "test_<short_name>.py", "content": "<full file source>"}}
  ]
}}
Every filename must end in .py and be a bare name (no directories).
"""


AGENT_TESTS_USER = """\
## Original issue
{issue_text}

## Proposed fix (unified diff)
{diff_block}

## Where the repo's tests live (for import-style reference)
{tests_tree}

Write the edge-case test files now (the JSON object only)."""


def render_agent_tests_prompt(
    issue_text: str,
    diff: str,
    tests_tree: str,
    max_tests: int = 3,
) -> List[Dict[str, str]]:
    """Messages list for the agent-tests generation call.

    Assumes diff is the candidate fix's unified diff ("" renders as an
    visibly-flagged empty diff) and tests_tree is a short listing of the
    repo's existing test files (import-style reference; "(none found)"
    is handled by the caller). max_tests bounds how many files the model
    is told to write — the harness enforces the same cap independently.
    """
    diff_block = diff.strip() or "(empty diff)"
    return [
        {"role": "system", "content": AGENT_TESTS_SYSTEM.format(max_tests=max_tests)},
        {
            "role": "user",
            "content": AGENT_TESTS_USER.format(
                issue_text=issue_text or "(none given)",
                diff_block=diff_block,
                tests_tree=tests_tree or "(none found)",
            ),
        },
    ]
