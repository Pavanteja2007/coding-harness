"""Research mode (Modes round, Task E) — read-only investigation with FETCH.

Investigates an unfamiliar library / approach / external topic and
reports a synthesized answer. Like Q&A mode this is read-only — no
sandbox, no edits, no verification loop — but the context comes from the
WEB (the FETCH tool's proven host-side reader) plus the repo when the
question touches it, plus the DOCS lookup layers (cache → pydoc → PyPI).

Design:

- **Read-only by construction**: research composes FETCH + DOCS + prompt
  assembly. No shell execution ever happens — FETCH/DOCS are control
  signals parsed on raw AND fence-stripped forms (same discipline as the
  fix-mode step loop); nothing touches the repo except read-only
  retrieval when the question names repo symbols.
- **The model drives fetches**: the answer call may output ``FETCH <url>``
  or ``DOCS <target>`` lines instead of a final answer; the harness
  executes the tool (host-side GET with SSRF guards / local docs layers),
  re-injects the result, and the model continues — up to a config budget,
  then a no-more-tools nudge guarantees termination.
- **Trace**: logs/{research-<id>}/trace.jsonl with task_start (mode=
  "research"), mode event, every web_fetch/docs_lookup (URL + outcome —
  the same audit discipline as fix mode), task_end.
- **No completion gate**: like Q&A, there is nothing to verify. The
  synthesized answer IS the deliverable.

Config keys (default-safe; FETCH bounds reuse the fix-mode keys):
- research_max_fetches (4) — FETCH budget per research task
- research_max_docs (4) — DOCS budget per research task
- research_turns (8) — total model round-trips (hard bound)
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness import docs_lookup as docs_lookup_mod
from harness import retrieval, webfetch
from harness.config import get_config
from harness.model_client import ModelClient
from harness.trace import TraceLogger

__all__ = ["RESEARCH_SYSTEM", "run_research"]

RESEARCH_SYSTEM = """\
You are a senior engineer researching an unfamiliar library, approach, or
external topic for the user, then reporting back a synthesized answer.

Available tools (output the line INSTEAD of an answer; the harness replies
with the result and you continue):

FETCH <full url including http:// or https://>
    Read one web page (read-only GET; readable text is extracted and
    returned, capped). Prefer specific, likely-stable pages (official
    docs, PyPI project pages, well-known guides).

DOCS <dotted target> [topic words]
    Look up installed-library documentation (local cache, then the
    interpreter's docs, then PyPI metadata when enabled).

Answer shape (when you have enough — or the budget forces it):
- A direct answer to the question first.
- Then 3-6 bullet key findings, each grounded in what you actually read
  (cite the URL / package + what it said).
- An honest "not found / uncertain" line for anything the sources did
  not settle. Never invent specifics (APIs, versions, benchmarks).

Rules:
- READ-ONLY research: no edits, no commands, nothing executed.
- A handful of FETCH/DOCS round-trips is plenty; the last turn must be
  the final answer (no tool lines in it).
"""


def _salvage_fetch_url(reply: str) -> Optional[str]:
    """Recover a FETCH URL from a DEGENERATE reply (endpoint flake).

    The documented failure mode: the model tries to fetch but glues the
    line ("assistantFETCH https://..." mid-paragraph), so no clean
    line-initial FETCH parses. When the reply mentions an explicit
    http(s) URL, salvage it: an attempted fetch must never silently
    degrade into an ungrounded "success". Returns the URL, or None.
    """
    m = re.search(r"https?://[^\s`\"')\]]+", reply or "")
    return m.group(0).rstrip(".,;") if m else None


def run_research(
    question: str,
    config: Optional[Dict[str, Any]] = None,
    log_root: Optional[Path] = None,
    repo_path: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Research a question with web/docs tools; return a synthesized answer.

    Returns {"answer", "task_id", "trace_path", "cost_usd", "model_calls",
    "fetches", "docs", "status"}. status "error" only on a model crash or
    empty question. Assumes config is the task/session config; repo_path is
    optional (a repo-flavored research question gets a retrieval block;
    a pure external question skips it). Read-only by construction — no
    shell, no sandbox, no edits, ever.
    """
    cfg = get_config(config or {})
    tid = task_id or f"research-{uuid.uuid4().hex[:8]}"
    root = Path(log_root) if log_root else Path(cfg.get("work_subdir", "logs"))
    trace = TraceLogger(root / tid)
    model = ModelClient(trace, cfg)
    docs_root = root / "_docs-cache"

    trace.log(
        "task_start",
        {
            "task_id": tid,
            "mode": "research",
            "repo_path": repo_path or "",
            "issue_text": question,
            "config": {k: v for k, v in cfg.items() if k != "api_key"},
        },
    )

    if not (question or "").strip():
        trace.log("task_end", {"status": "error", "reason": "empty question"})
        return {
            "answer": "",
            "task_id": tid,
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
            "cost_usd": 0.0,
            "model_calls": [],
            "fetches": [],
            "docs": [],
            "status": "error",
        }

    # -- optional repo context (read-only retrieval) --------------------
    repo_block = ""
    if repo_path and Path(repo_path).is_dir():
        ctx = retrieval.retrieve_context(
            repo_path,
            question,
            max_files=int(cfg.get("qa_max_files", 4)),
            target_test=None,
            index_root=root / "_code-graph",
        )
        if ctx["files"]:
            head = "\n".join(f"- {f}" for f in ctx["files"][:4])
            repo_block = (
                "## Repo context (files the question's terms matched)\n"
                f"{head}\n(Background only — this is research, not a fix.)\n\n"
            )
            trace.log(
                "retrieval", {"strategy": ctx.get("strategy"), "files": ctx["files"]}
            )

    trace.log(
        "mode",
        {
            "mode": "research",
            "question": question,
            "web_fetch_enabled": bool(cfg.get("web_fetch_enabled", True)),
        },
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": RESEARCH_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## Research question\n{question}\n\n"
                f"{repo_block}"
                "Research it now (FETCH/DOCS lines as needed, then the "
                "final answer)."
            ),
        },
    ]

    max_fetches = int(cfg.get("research_max_fetches", 4))
    max_docs = int(cfg.get("research_max_docs", 4))
    max_turns = int(cfg.get("research_turns", 8))
    fetches: List[Dict[str, Any]] = []
    docs_used: List[str] = []
    fetch_budget = max_fetches
    docs_budget = max_docs
    answer = ""

    def _audit_fetch(res, url: str) -> None:
        fetches.append({"url": url, "ok": res.ok, "status": res.status})
        trace.log(
            "web_fetch",
            {"url": url, "ok": res.ok, "status": res.status, "chars": len(res.text)},
        )

    try:
        reply = model.call(messages, step="research")
        for _turn in range(max_turns):
            lines = [ln.strip() for ln in (reply or "").splitlines() if ln.strip()]
            tool_lines = [
                ln
                for ln in lines
                if webfetch.parse_fetch(ln) is not None
                or docs_lookup_mod.parse_docs(ln) is not None
            ]

            # Degenerate-fetch salvage: the reply has no CLEAN tool line
            # but does name an explicit URL (the glued "assistantFETCH
            # https://..." endpoint flake). An attempted fetch must not
            # silently degrade into an ungrounded answer — execute the
            # salvage URL as the tool line it was trying to be.
            if (
                not tool_lines
                and fetch_budget > 0
                and "FETCH" in (reply or "").upper()
                and cfg.get("web_fetch_enabled", True)
            ):
                url = _salvage_fetch_url(reply or "")
                if url:
                    trace.log(
                        "research_fetch_salvage",
                        {"url": url, "reason": "degenerate FETCH line"},
                    )
                    tool_lines = [f"FETCH {url}"]
                    reply = tool_lines[0]  # re-enter as a clean tool line

            if not tool_lines:
                answer = (reply or "").strip()
                break

            # execute the tool lines (bounded), re-inject results
            bodies: List[str] = []
            forced_final = False
            for ln in tool_lines[:3]:
                url = webfetch.parse_fetch(ln)
                if url is not None:
                    if not cfg.get("web_fetch_enabled", True):
                        bodies.append(
                            "FETCH is disabled for this task — answer from "
                            "what you already have."
                        )
                        continue
                    if fetch_budget <= 0:
                        forced_final = True
                        continue
                    fetch_budget -= 1
                    msg, _res = webfetch.fetch_and_render(
                        url,
                        timeout_s=int(cfg.get("webfetch_timeout_s", 15)),
                        max_bytes=int(cfg.get("webfetch_max_bytes", 1_048_576)),
                        max_chars=int(cfg.get("webfetch_max_chars", 3000)),
                        max_redirects=int(cfg.get("webfetch_max_redirects", 3)),
                        audit_hook=lambda r, _u=url: _audit_fetch(r, _u),
                    )
                    bodies.append(msg)
                    continue
                dq = docs_lookup_mod.parse_docs(ln)
                if dq is not None:
                    if docs_budget <= 0:
                        forced_final = True
                        continue
                    docs_budget -= 1
                    docs_used.append(dq)
                    dmsg, dres = docs_lookup_mod.lookup_and_render(
                        dq,
                        docs_root,
                        max_chars=int(cfg.get("docs_max_chars", 3000)),
                        allow_remote=bool(cfg.get("docs_lookup_allow_remote", False)),
                    )
                    trace.log(
                        "docs_lookup",
                        {"query": dq, "source": dres.source, "ok": dres.ok},
                    )
                    bodies.append(dmsg)
            if not bodies or (fetch_budget <= 0 and docs_budget <= 0):
                forced_final = True
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "\n\n".join(bodies)
                        if bodies and not forced_final
                        else (
                            "\n\n".join(bodies)
                            + "\n\nTool budget exhausted — produce the FINAL "
                            "answer now (no more FETCH/DOCS lines)."
                        )
                    ),
                }
            )
            reply = model.call(messages, step="research")
            if forced_final:
                answer = (reply or "").strip()
                break
        else:
            answer = (reply or "").strip()
        if not answer:
            # last turn was a tool request the budget refused: force a
            # final answer with one more call (bounded, cannot loop)
            messages.append({"role": "assistant", "content": reply or ""})
            messages.append(
                {
                    "role": "user",
                    "content": "Produce the FINAL answer now (no more "
                    "FETCH/DOCS lines).",
                }
            )
            answer = (model.call(messages, step="research-final") or "").strip()
        if not answer:
            # An EMPTY reply (the endpoint's reasoning-burn flake) is NOT
            # an answer: ONE retry with an explicit repair nudge (a
            # different ask — the same prompt deterministically burns
            # again on this endpoint class), then an honest error —
            # success must never be minted on "" (same discipline as
            # qa_mode).
            trace.log("research_empty_reply_retry", {})
            messages.append({"role": "assistant", "content": ""})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous reply came back EMPTY. Produce the "
                        "final answer now, as plain text (direct answer "
                        "first, then 3-6 grounded key findings, then an "
                        "honest not-found line). No more FETCH/DOCS lines."
                    ),
                }
            )
            answer = (model.call(messages, step="research-final-retry") or "").strip()
    except Exception as exc:
        trace.log("task_end", {"status": "error", "reason": f"model failed: {exc}"})
        return {
            "answer": "",
            "task_id": tid,
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
            "cost_usd": model.total_cost_usd,
            "model_calls": list(model.model_calls),
            "fetches": fetches,
            "docs": docs_used,
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
            "fetches": fetches,
            "docs": docs_used,
            "status": "error",
        }

    trace.log("task_end", {"status": "success", "mode": "research"})
    return {
        "answer": answer,
        "task_id": tid,
        "trace_path": str((root / tid / "trace.jsonl").resolve()),
        "cost_usd": model.total_cost_usd,
        "model_calls": list(model.model_calls),
        "fetches": fetches,
        "docs": docs_used,
        "status": "success",
    }
