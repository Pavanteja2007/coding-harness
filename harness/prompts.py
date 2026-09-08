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
      "files_hint": ["file1.py", "file2.py"]
    }
  ]
}

Rules:
- No more than 4 sub-steps; prefer fewer for small bugs.
- Do not include a step "run the full test suite" — the harness verifies
  automatically after every step.
- files_hint: repo-relative paths you believe must change or be read.
"""

PLANNER_USER = """\
## Issue
{issue_text}

## Retrieved context ({strategy} — may be incomplete)
{context_block}

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

## Step rules
- One bash command per turn (you may compose with &&).
- Do not modify test files unless the issue explicitly says the TEST is \
wrong; fix the CODE, not the tests.
- Keep edits minimal and focused on this step only.
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


def issue_one_line(issue_text: str, width: int = 200) -> str:
    """First line-ish compression of the issue for re-injection blocks."""
    flat = " ".join((issue_text or "").split())
    return flat[: width - 1] + "…" if len(flat) > width else flat


def render_planner_prompt(
    issue_text: str,
    context_block: str,
    constraints_block: str,
    strategy: str = "grep",
) -> List[Dict[str, str]]:
    """Messages list for the planner call. Assumes context_block and
    constraints_block are pre-rendered strings; `strategy` describes how
    the context was retrieved (shown to the model so it can weigh the
    context's reliability)."""
    return [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": PLANNER_USER.format(
            issue_text=issue_text or "(none given)",
            context_block=context_block or "(none)",
            constraints_block=constraints_block or "(none)",
            strategy=strategy or "grep",
        )},
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
