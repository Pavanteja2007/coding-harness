"""Question/Q&A mode (Modes round, Task C) — read-only, no sandbox, no edits.

Answers a question about THIS codebase (or a general question answerable
from repo context) by assembling what the harness already has — the
structural+grep retrieval layer, the decision-memory store, the shared
code-graph — and asking the model with a Q&A system prompt. The model's
answer is returned verbatim; nothing is executed, edited, or verified.

Design (deliberately minimal — this mode is "assemble context, answer"):

- **No working copy, no pristine snapshot**: read-only modes never copy
  the repo. Retrieval and memory read the ORIGINAL repo directly (they
  are read-only by construction — same as the fix-mode planner).
- **Trace**: a per-question trace dir logs/{qa-<id>}/trace.jsonl with
  kind "task_start" (mode="question") so sessions/traceview see it;
  a `mode` event records {mode: "question", question, files, memory}.
- **Read-only READ signal**: the model is offered ``READ <repo-relative
  path>`` (mirroring FETCH/DOCS discipline — a control signal, never
  executed as shell) to pull a specific file's content into the answer
  context, capped. Everything runs on the HOST against the original
  repo, read-only; there is no sandbox because there is no execution.
- **No completion gate**: there is nothing to verify. The answer's
  quality surface is the answer itself.

Config keys (all default-safe):
- qa_max_files (4) — retrieval files injected
- qa_context_lines (80) — lines per file in the context block
- qa_max_reads (6) — READ budget per question
- qa_max_read_chars (4000) — cap per READ result
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness import decision_memory, retrieval
from harness.config import get_config
from harness.model_client import ModelClient
from harness.trace import TraceLogger

__all__ = ["QA_SYSTEM", "run_question"]

QA_SYSTEM = """\
You are a senior engineer answering a question about ONE repository (read-only).

You get: the question, a set of relevant files (ranked by structural +
keyword retrieval against the repo), their matching lines, and possibly
past decisions recorded from earlier work in this repo. Answer the
question directly and concisely, grounded in the provided context — cite
file paths (and function/class names) for every claim. If the context
does not contain the answer, say so honestly and answer from general
knowledge only when clearly labeled as such.

You may request one more file's content by outputting, on its own line:
READ <repo-relative path>
The harness replies with that file's content (capped). Use it sparingly.

Rules:
- READ-ONLY: you are describing code, never proposing to edit it here.
- Cite paths like numlib/mathutil.py for concrete claims.
- Prefer a short, direct answer over an essay.
"""


def _render_context(
    repo_path: str,
    ctx: Dict[str, Any],
    max_files: int,
    max_lines: int,
) -> str:
    """Render the retrieval context block for the QA prompt."""
    parts: List[str] = []
    for rel in (ctx.get("files") or [])[:max_files]:
        p = Path(repo_path, rel)
        try:
            if not p.is_file() or p.stat().st_size > 200_000:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        if len(lines) > max_lines:
            shown = [
                *lines[:max_lines],
                f"... [{len(lines) - max_lines} more lines]",
            ]
        else:
            shown = lines
        parts.append(f"### {rel}\n```\n" + "\n".join(shown) + "\n```")
    if not parts:
        return "(retrieval found no clearly relevant files)"
    return "\n\n".join(parts)


def _read_file(repo_path: str, rel: str, max_chars: int) -> str:
    """Render one READ result: the file's content, capped.

    Assumes rel is a repo-relative path parsed from the model's READ
    line (already stripped of quotes/backticks). Path-traversal shaped
    requests are refused (the read stays inside the repo). Never raises
    — a miss is a message, not a crash.
    """
    clean = (rel or "").strip().strip("`\"'")
    if not clean or ".." in Path(clean).parts or Path(clean).is_absolute():
        return f"READ refused: {rel!r} is not a safe repo-relative path."
    p = Path(repo_path, clean)
    try:
        if not p.is_file():
            return f"READ miss: {clean} does not exist in this repo."
        if p.stat().st_size > 200_000:
            return f"READ miss: {clean} is too large to inline."
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"READ miss: {clean} could not be read ({exc})."
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…[truncated]"
    return f"READ {clean}:\n```\n{text}\n```"


_READ_PAT = re.compile(r"^\s*READ\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)


def parse_read(text: str) -> Optional[str]:
    """Return the path of a READ request, or None if the message isn't one.

    Same control-signal discipline as FETCH/DOCS: parsed before any
    command extraction; a READ line is never executed as shell.
    """
    m = _READ_PAT.match((text or "").strip())
    if not m:
        return None
    raw = m.group(1).strip().strip("`\"'")
    if not raw:
        # no argument after READ — not a control line
        return None
    return raw


def run_question(
    question: str,
    repo_path: str,
    config: Optional[Dict[str, Any]] = None,
    log_root: Optional[Path] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Answer a question about the repo — read-only, no sandbox.

    Returns {"answer", "task_id", "trace_path", "cost_usd", "model_calls",
    "files", "status"}. status is "success" when a model answer was
    produced, "error" otherwise (model crash/empty question). Assumes
    repo_path is a readable directory and config is the task/session
    config dict (unknown keys pass through). NEVER mutates the repo —
    retrieval/memory read it directly, both read-only by construction.
    """
    cfg = get_config(config or {})
    tid = task_id or f"qa-{uuid.uuid4().hex[:8]}"
    root = Path(log_root) if log_root else Path(cfg.get("work_subdir", "logs"))
    trace = TraceLogger(root / tid)
    model = ModelClient(trace, cfg)

    trace.log(
        "task_start",
        {
            "task_id": tid,
            "mode": "question",
            "repo_path": repo_path,
            "issue_text": question,
            "config": {k: v for k, v in cfg.items() if k != "api_key"},
        },
    )

    if not (question or "").strip() or not Path(repo_path).is_dir():
        trace.log("task_end", {"status": "error", "reason": "empty question or repo"})
        return {
            "answer": "",
            "task_id": tid,
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
            "cost_usd": 0.0,
            "model_calls": [],
            "files": [],
            "status": "error",
        }

    # -- assemble context: retrieval + decision memory (both read-only) --
    ctx = retrieval.retrieve_context(
        repo_path,
        question,
        max_files=int(cfg.get("qa_max_files", 4)),
        target_test=None,
        index_root=root / "_code-graph",
    )
    mem = decision_memory.query_planning_decisions(
        repo_path=repo_path,
        issue_text=question,
        retrieval_terms=ctx["terms"],
        limit=int(cfg.get("memory_query_limit", 6)),
    )
    memory_block = decision_memory.render_memory_block(
        mem["decisions"], max_chars=int(cfg.get("memory_max_chars", 1500))
    )
    trace.log(
        "mode",
        {
            "mode": "question",
            "question": question,
            "files": ctx["files"],
            "memory_matched": len(mem["decisions"]),
        },
    )

    context_block = _render_context(
        repo_path,
        ctx,
        int(cfg.get("qa_max_files", 4)),
        int(cfg.get("qa_context_lines", 80)),
    )
    greps = "\n".join(
        f"- {f}: {lines[0] if lines else '(no direct term hits)'}"
        for f, lines in list((ctx.get("greps") or {}).items())[:4]
    )
    messages = [
        {"role": "system", "content": QA_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## Question\n{question}\n\n"
                f"## Retrieved context ({ctx.get('strategy', 'grep')} — may be "
                f"incomplete)\n{context_block}\n\n"
                f"## Matching lines (first hit per file)\n{greps or '(none)'}\n\n"
                f"## Past decisions in this repo\n{memory_block}\n\n"
                "Answer the question now."
            ),
        },
    ]

    # -- up to N READ round-trips, then the final answer -----------------
    reads_used = 0
    max_reads = int(cfg.get("qa_max_reads", 6))
    answer = ""
    try:
        reply = model.call(messages, step="answer")
        while reads_used < max_reads:
            reads = [ln.strip() for ln in (reply or "").splitlines() if parse_read(ln)]
            if not reads:
                break
            reads_used += 1
            bodies = [
                _read_file(
                    repo_path, parse_read(ln), int(cfg.get("qa_max_read_chars", 4000))
                )
                for ln in reads[:3]
            ]
            trace.log("qa_read", {"paths": [parse_read(ln) for ln in reads[:3]]})
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": "\n\n".join(bodies)
                    + "\n\nAnswer the question now (no more READs needed).",
                }
            )
            reply = model.call(messages, step="answer")
        answer = (reply or "").strip()
        if not answer:
            # An EMPTY reply (the endpoint's documented reasoning-burn
            # flake — tokens spent as hidden reasoning, content=None) is
            # NOT an answer. ONE retry with an explicit repair nudge
            # (a different ask — the same prompt deterministically burns
            # again on this endpoint class), then an honest error:
            # success must never be minted on "".
            trace.log("qa_empty_reply_retry", {})
            messages.append({"role": "assistant", "content": ""})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous reply came back EMPTY. Answer the "
                        "question again now, as plain text (3-8 sentences, "
                        "cite the relevant file paths). No reasoning steps, "
                        "just the answer."
                    ),
                }
            )
            reply = model.call(messages, step="answer-retry")
            answer = (reply or "").strip()
    except Exception as exc:
        trace.log("task_end", {"status": "error", "reason": f"model failed: {exc}"})
        return {
            "answer": "",
            "task_id": tid,
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
            "cost_usd": model.total_cost_usd,
            "model_calls": list(model.model_calls),
            "files": ctx["files"],
            "status": "error",
        }
    if not answer:
        trace.log(
            "task_end",
            {"status": "error", "reason": "model returned an empty answer twice"},
        )
        return {
            "answer": "",
            "task_id": tid,
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
            "cost_usd": model.total_cost_usd,
            "model_calls": list(model.model_calls),
            "files": ctx["files"],
            "status": "error",
        }

    trace.log("task_end", {"status": "success", "mode": "question"})
    return {
        "answer": answer,
        "task_id": tid,
        "trace_path": str((root / tid / "trace.jsonl").resolve()),
        "cost_usd": model.total_cost_usd,
        "model_calls": list(model.model_calls),
        "files": ctx["files"],
        "status": "success",
    }
