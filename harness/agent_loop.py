"""General coding-agent loop — the interactive `neo` engine (not `neo fix`).

`harness.core.run_task` is the verifier-gated benchmark path: pristine/work
snapshots, planner, verifier-gated success. That path is UNCHANGED and stays
the implementation behind `neo fix`.

This module is the OTHER engine: a generic tool-calling loop that works on
the LIVE repo copy (the user's cwd), for daily work — explain code, add
features, refactor, run commands, debug failures iteratively. Bug-fix is ONE
task type here, not the product:

- classify -> (question | agent_task | chit_chat). Pure questions are
  answered read-only (retrieval + memory, no tools that mutate); chit-chat
  gets an inline reply and launches nothing; everything else is ONE
  agent-task loop (fix/build/refactor/run/debug share it — not 4 modes).
- The loop is tool-calling, multi-turn: READ/GLOB/GREP/BASH/EDIT/WRITE +
  MEMORY, until the model emits DONE. Steering (harness.steering) is polled
  every turn: guide injects into the live session, abort stops cleanly,
  replan is treated as strong guidance (there is no fixed plan to replace).
- NO verifier gate by default: success = the model finished the task.
  The verifier runs only when the task declares tests (config target_test
  or test_command) or the model explicitly calls VERIFY.
- Edits happen IN PLACE on the live repo. A pristine snapshot at
  logs/{task_id}/pristine exists ONLY as the diff/undo reference (plus
  per-file originals under logs/{task_id}/orig/) — the loop never builds
  in a work/ copy.
- Permission model: reads are always allowed; BASH/EDIT/WRITE need
  approval only when config agent_approval="require" (via the approve_fn
  callback). Otherwise they run and the diff is shown after.

Trace compatibility: the loop emits the SAME public trace kinds the CLI
already renders (task_start, retrieval, model_request/response, tool_call,
tool_result, verify, steering*, task_end, result) plus two additive ones
(edit_applied, approval_*). cli.tracelog.FeedBuilder renders tool_call
lines (agent verbs are classified there); cli.runview reads result/task_end
the same way. Nothing here changes the fix-mode schema.
"""

from __future__ import annotations

import glob as _globmod
import hashlib
import inspect
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from harness import agent_loop_step as step_mod
from harness import skipset, tool_errors, turn_caps
from harness import steering as steering_mod
from harness.agent_kernel import (
    AgentKernel,
    RunResult,
    RunSpec,
    SessionController,
    resolve_agent_strategy,
)
from harness.config import get_config
from harness.model_client import ModelClient
from harness.trace import TraceLogger

__all__ = [
    "AGENT_DEFAULT_STRATEGY_KEY",
    "AgentIntent",
    "AgentKernel",
    "AgentResult",
    "HarnessToolbox",
    "RunResult",
    "RunSpec",
    "SessionController",
    "agent_diff",
    "classify_agent_input",
    "classify_deterministic",
    "load_resume_history",
    "parse_tool_call",
    "render_agent_plan",
    "resolve_agent_dispatch",
    "route_tool",
    "run_agent",
    "run_agent_kernel",
    "run_agent_legacy",
    "run_agent_stepped",
    "undo_edits",
]

#: AGT-11: the strategy name that selects the pure-step engine. It is an
#: explicit pin, never a default -- ``harness.agent_loop_step.step`` is the
#: new canonical core, but switching every run onto it is a separate, measured
#: decision (see ``harness/AGENTS.md`` AGT-11 for what is and is not migrated).
STEPPED_STRATEGY = "agent_step"

AGENT_SYSTEM = """\
You are Neo, a general coding agent working LIVE in the user's repository.
You have tools; use them, one per reply, until the task is done.

TOOLS (exactly one call per reply):
- JSON form (preferred): {"tool": "read"|"glob"|"grep"|"bash"|"edit"|"write"|"memory"|"fetch"|"mcp"|"verify"|"done", ...args}
  - read: {"tool":"read","path":"src/x.py"} — read a repo-relative file
  - glob: {"tool":"glob","pattern":"**/*.py"} — list matching files
  - grep: {"tool":"grep","pattern":"def mean","path":"src"} — search content
  - bash: {"tool":"bash","command":"python -m pytest tests/ -x -q"} — run a shell command LIVE in the repo
  - edit: {"tool":"edit","path":"src/x.py","old_string":"...","new_string":"..."} — replace one exact block (first match)
  - write: {"tool":"write","path":"src/new.py","content":"..."} — create/overwrite a whole file
  - memory: {"tool":"memory","query":"pytest conventions"} — past decisions for this repo
  - fetch: {"tool":"fetch","url":"https://docs.example.com/x"} — read one web page (GET-only, capped)
  - mcp: {"tool":"mcp","server":"<label>","name":"<tool>","args":{}} — call an installed MCP server tool
  - verify: {"tool":"verify"} — run the declared tests (only meaningful when tests are declared)
  - done: {"tool":"done","answer":"summary of what was done"} — finish
  - plugin verbs: {"tool":"<verb>","command":"<rest of command>"} — an installed plugin's tool verb
- Plain-text form (same tools, one line): READ <path> | GLOB <pattern> |
  GREP <pattern> [path] | BASH <command> | MEMORY <query> | FETCH <url> | VERIFY | DONE [answer text]
  For EDIT/WRITE/MCP always use the JSON form (exact strings matter).

RULES:
- Explore before editing: READ/GLOB/GREP first for unfamiliar code.
- Make the SMALLEST change that satisfies the request; never touch tests/ unless asked.
- After edits that should work, verify with BASH (run the relevant tests/command) before DONE.
- Keep replies short: one tool call, no essay around it. Explanations go in the DONE answer.
"""


# ---------------------------------------------------------------------------
# Classification: question | agent_task | chit_chat
# ---------------------------------------------------------------------------


@dataclass
class AgentIntent:
    """Outcome of the agent dispatcher.

    kind: "question" (read-only answer, no mutations), "agent_task"
    (the tool loop — fix/build/refactor/run/debug), "chit_chat"
    (inline reply, nothing launched). reply carries the inline text for
    chit_chat (clarifying question when unsure). used_model mirrors the
    harness.intent tier flag.
    """

    kind: str
    reason: str = ""
    reply: str = ""
    used_model: bool = False


_CHIT_CHAT_OPENERS = {
    "hi",
    "hello",
    "hey",
    "yo",
    "hiya",
    "howdy",
    "sup",
    "thanks",
    "thank you",
    "ty",
    "thx",
    "cheers",
    "ok",
    "okay",
    "cool",
    "nice",
    "great",
    "awesome",
    "lol",
    "haha",
    "bye",
    "goodbye",
}

_META_PATTERNS = (
    r"what can you do",
    r"who are you",
    r"what are you",
    r"how do (i|you) (use|quit|exit|stop|cancel)",
    r"how does (this|it|neo) work",
    r"what does (this|it|neo) do",
    r"show me (your|the) (commands|help)",
    r"what commands",
    r"which commands",
    r"are you (an? )?(ai|agent|llm|bot|robot)",
    r"what model",
    r"which model",
)

# "explain X", "describe X", "show me X", "tell me about X" are questions
# even without a question mark (the harness.intent starter table only
# matches how/what/why/... at the start).
_EXPLAIN_RE = re.compile(
    r"^\s*(explain|describe|walk\s+me\s+through|tell\s+me\s+about|"
    r"show\s+me|what\s|how\s|why\s|where\s|which\s|who\s|when\s|"
    # "can u tell me the best mouse ...", "tell me which ...", "suggest ..."
    # are knowledge questions. Without these they matched nothing, carried no
    # "?", and fell through to the dead-end fallback below.
    r"(can\s+(you|u|we)\s+)?(tell\s+me|suggest|recommend|advise)\b|"
    r"(is|are|whats|whats)\s+(there\s+)?(a\s+)?(good|best)\b)",
    re.IGNORECASE,
)

# Shell/run verbs are always agent tasks, never questions.
_RUN_RE = re.compile(
    r"^\s*(run|execute|rerun|re-run)\b|\b(run|execute)\s+(pytest|python|npm|node|git|ls|cat|rg|grep|make|ruff|mypy)\b",
    re.IGNORECASE,
)

# Strong fix/debug verbs: something exists and must be repaired.
_FIX_RE = re.compile(
    r"\b(refactor|rename|extract|inline|clean\s*up|organize|reorganize|"
    r"fix|repair|patch|update|change|modify|migrate|remove|delete|"
    r"debug|repro|investigate\s+the\s+(bug|crash|fail|failure))\b",
    re.IGNORECASE,
)

# Build verbs: something new is wanted. Bare "add/create/..." with an
# object is a task; inside a pure question ("what should I add?") the
# question shape wins (checked first).
#
# `design` and `make` were MISSING, which sent two of the most ordinary
# requests a user can type straight to the dead-end fallback:
#   "design a web based game ..."  -> chit_chat
#   "make a mario type game"       -> chit_chat
# Both are unambiguous build requests. The same gap applied to develop/code/
# program/scaffold/prototype. `make` is safe here because a question shape is
# resolved BEFORE this regex is consulted, so "what makes it slow" is already
# a question by the time we get here.
_BUILD_RE = re.compile(
    r"\b(add|create|build|implement|generate|introduce|extend|support|write|"
    r"design|make|develop|code|program|scaffold|prototype|craft|set\s+up)\b",
    re.IGNORECASE,
)

# Research markers: read-only investigation of an external topic (web/docs
# territory). Handled as a question — the read-only answer path.
_RESEARCH_RE = re.compile(
    r"\b(research(ing|es)?|investigate|look\s+into|look\s+up|find\s+out|"
    r"compare[sd]?|comparison|alternatives?|best\s+(library|package|"
    r"framework|tool)|which\s+(library|package|framework))\b",
    re.IGNORECASE,
)

_QUESTION_MARKERS = re.compile(
    r"\b(what\s+does|how\s+does|how\s+do|why\s+does|where\s+is|"
    r"what\s+is|what\s+are|explain|describe)\b",
    re.IGNORECASE,
)


def classify_deterministic(text: str) -> AgentIntent:
    """Rule-only agent classification; "unknown" means ask the model tier.

    Assumes a single typed line. Never raises; empty input is chit_chat
    with a clarifying nudge (the session loop already skips blanks).
    """
    stripped = (text or "").strip()
    if not stripped:
        return AgentIntent("chit_chat", "empty input", "what would you like to do?")
    low = stripped.lower()
    words = re.findall(r"[a-z']+", low)
    first = words[0] if words else ""

    if (
        first in _CHIT_CHAT_OPENERS
        and len(words) <= 4
        # "hi", "thanks", "ok" — but "ok run the tests" is a task.
        and not _RUN_RE.search(stripped)
        and not _FIX_RE.search(stripped)
        and not _BUILD_RE.search(stripped)
    ):
        return AgentIntent("chit_chat", "greeting/ack", "hey — what are we working on?")
    if any(re.search(p, low) for p in _META_PATTERNS):
        return AgentIntent(
            "chit_chat",
            "meta question about neo",
            "I'm neo — I work on this repo with you: explain code, make changes, "
            "run commands, debug failures. Just say what you need.",
        )
    if re.match(r"^\s*(help\s+me|help)\s*[.!?]*\s*$", stripped, re.IGNORECASE):
        return AgentIntent(
            "chit_chat",
            "bare help",
            "tell me what to do — e.g. 'explain how routing works', "
            "'add logging to X', or 'run pytest and fix failures'.",
        )

    # Explicit run/shell work is always a task.
    if _RUN_RE.search(stripped):
        return AgentIntent("agent_task", "run/shell verb")
    # Strong fix/debug verbs win over everything else below: a failure
    # described anywhere ("research the crash") is a task, not reading.
    if _FIX_RE.search(stripped):
        return AgentIntent("agent_task", "code-change/action verb")
    # Read-only investigation of an external topic -> question.
    if _RESEARCH_RE.search(stripped):
        return AgentIntent("question", "research/investigation request")
    # Question-shaped + no action verbs -> question. "explain how routing
    # works" lands here; "run pytest and explain failures" stays a task
    # (the run verb already returned above).
    is_q = stripped.rstrip().endswith("?") or bool(_EXPLAIN_RE.match(stripped))
    if is_q:
        return AgentIntent("question", "explanation request, no action verbs")
    # New-capability verbs with an object -> the one loop.
    if _BUILD_RE.search(stripped):
        return AgentIntent("agent_task", "build verb with an object")
    # Source artifacts with a declarative sentence ("mean() in mathutil.py
    # returns the sum") read as a task, not a question.
    if re.search(r"[A-Za-z_][A-Za-z0-9_]{2,}\(\)|\.py\b|src/|tests?/|::", stripped):
        if not is_q:
            return AgentIntent("agent_task", "source artifact + declarative")
        return AgentIntent("question", "question about named code")
    if is_q:
        return AgentIntent("question", "question shape")
    # Genuinely unrecognised input ASKS rather than guessing. That is the
    # deliberate cost asymmetry: a wrong run burns minutes and model budget,
    # while a clarifying question costs one line. (An earlier attempt made this
    # `agent_task` for any input of 3+ words; that misrouted nonsense like
    # "florp the wobble" into a real run, and it broke the pinned behaviour in
    # tests/test_agent_loop.py::test_gray_zone_model_failure_asks.)
    #
    # The REAL complaint was never that nonsense asks -- it was that the reply
    # attached here was a GREETING ("hey - what are we working on?"), which
    # reads as if the agent thought you were saying hello. So the reply now
    # actually asks the question, and names the two ways to proceed.
    return AgentIntent(
        "chit_chat",
        "unrecognised request",
        "I'm not sure what you want me to do with that. I can answer a "
        "question, or change code in this repo. Which one did you mean?",
    )


_AGENT_MODEL_SYSTEM = """\
You route ONE user message for a coding agent in a repo. Reply with STRICT JSON only:
{"kind": "question"|"agent_task"|"chit_chat", "reason": "<1 short sentence>"}
- "question": wants an explanation of code/behavior, no changes, no commands.
- "agent_task": wants code changed, commands run, failures debugged, refactoring.
- "chit_chat": greetings, thanks, meta questions, or genuinely unclear input.
"""


def _parse_agent_reply(raw: str) -> Optional[str]:
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        word = (raw or "").strip().strip("`\"' .!").lower()
        return word if word in ("question", "agent_task", "chit_chat") else None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    kind = obj.get("kind") if isinstance(obj, dict) else None
    if isinstance(kind, str):
        kind = kind.strip().lower()
        if kind in ("question", "agent_task", "chit_chat"):
            return kind
    return None


def classify_agent_input(
    text: str,
    config: Optional[Dict[str, Any]] = None,
    trace: Any = None,
) -> AgentIntent:
    """Full two-tier agent classification.

    Tier 1 is deterministic (offline, free). Tier 2 (ONE cheap model
    call, difficulty_hint="easy") covers only the "unknown" gray zone.
    config honors agent_intent_enabled=False (everything is agent_task,
    the legacy every-input-works behavior) and intent_model. Never raises:
    any model failure degrades to chit_chat with a clarifying reply.
    """
    cfg = config or {}
    if not cfg.get("agent_intent_enabled", True):
        return AgentIntent("agent_task", "agent_intent_enabled=False")
    it = classify_deterministic(text)
    if it.kind != "chit_chat" or it.reason != "unknown":
        if trace is not None:
            try:
                trace.log(
                    "intent", {"kind": it.kind, "reason": it.reason, "tier": "rules"}
                )
            except Exception:
                pass
        return it
    # Gray zone: one cheap model call.
    from harness.deps import get_call_model

    try:
        fn = get_call_model()
        call_cfg = dict(cfg)
        if call_cfg.get("intent_model"):
            call_cfg["model"] = call_cfg["intent_model"]
        messages = [
            {"role": "system", "content": _AGENT_MODEL_SYSTEM},
            {
                "role": "user",
                "content": f"Message:\n{text}\n\nReply with the JSON verdict now.",
            },
        ]
        raw = fn(
            messages,
            difficulty_hint="easy",
            provider=call_cfg.get("provider"),
            model=call_cfg.get("model"),
            api_key=call_cfg.get("api_key"),
        )
        kind = _parse_agent_reply(raw)
        if kind is None:
            return AgentIntent(
                "chit_chat",
                "model reply unparseable",
                "not sure what you want — explain, change, or run something?",
                used_model=True,
            )
        if trace is not None:
            try:
                trace.log(
                    "intent", {"kind": kind, "reason": "model tier", "tier": "model"}
                )
            except Exception:
                pass
        return AgentIntent(kind, "model tier", used_model=True)
    except Exception as exc:
        return AgentIntent(
            "chit_chat",
            f"model tier failed: {exc}",
            "not sure what you want — explain, change, or run something?",
        )


# ---------------------------------------------------------------------------
# Tool parsing (model reply -> one tool call)
#
# AGT-11: the grammar MOVED to `harness.agent_loop_step` -- the pure decision
# core -- and is re-exported here unchanged. The parser is a pure function of
# one string, so it belongs beside the function that CALLS it rather than
# inside the adapter that supplies the effects. Every existing import site
# (`from harness.agent_loop import parse_tool_call`) keeps working; a
# differential against the pre-move implementation over 35 hand-written
# replies x two verb sets plus every string of length <= 3 over a
# grammar-shaped alphabet reported 0 divergences.
# ---------------------------------------------------------------------------

_TOOL_NAMES = step_mod.TOOL_NAMES
_PLAIN_PATTERNS = step_mod.PLAIN_PATTERNS
_strip_fences = step_mod.strip_fences
parse_tool_call = step_mod.parse_tool_call


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class AgentResult(dict):
    """run_agent outcome (a dict with an .ok helper)."""

    @property
    def ok(self) -> bool:
        return bool(
            self.get("status")
            in {"success", "completed_verified", "completed_unverified"}
        )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

_SKIP_DIRS = skipset.SKIP_DIRS


def _safe_agent_id(value: Any) -> str:
    """Return a contained directory name for an agent task identifier."""
    text = str(value or "")
    if (
        text
        and text == text.strip()
        and not any(ch in text for ch in '/\\:*?"<>|')
        and text.rstrip(". ") not in ("", ".", "..")
    ):
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"invalid-agent-{digest}"


def _safe_join(repo: Path, rel: str) -> Optional[Path]:
    clean = (rel or "").strip().strip("`\"'")
    if not clean:
        return None
    p = Path(clean)
    if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:[\\/]", clean):
        return None
    try:
        full = (repo / p).resolve()
        if full != repo.resolve() and repo.resolve() not in full.parents:
            return None
        return full
    except OSError:
        return None


def _render_tool_command(tool: str, args: Dict[str, Any]) -> str:
    """Human/shell-ish rendering of a tool call for tool_call trace events.

    Kept close to real shell so cli.tracelog.classify_command produces
    the familiar Reading/Searching/Running/Editing texture without a
    second logging path.
    """
    if tool == "read":
        return f"cat {args.get('path', '')}"
    if tool == "glob":
        return f"ls {args.get('pattern', '')}"
    if tool == "grep":
        pat = args.get("pattern", "")
        path = args.get("path", "")
        return f"rg {pat} {path}".strip()
    if tool == "bash":
        return str(args.get("command", ""))
    if tool == "edit":
        return f"EDIT {args.get('path', '')}"
    if tool == "write":
        return f"WRITE {args.get('path', '')}"
    if tool == "memory":
        return f"MEMORY {args.get('query', '')}"
    if tool == "fetch":
        return f"FETCH {args.get('url', '')}"
    if tool in ("mcp", "mcp_call"):
        return f"MCP {args.get('server', '')} {args.get('name', '')}".strip()
    if tool in ("mcp", "mcp_call"):
        return f"MCP {args.get('server', '')} {args.get('name', '')}".strip()
    if tool == "verify":
        return "VERIFY"
    if tool == "done":
        return "DONE"
    # Plugin verbs render as the shell line they will run.
    if args.get("command"):
        return f"{tool.upper()} {args.get('command', '')}".strip()
    return tool.upper()


# ---------------------------------------------------------------------------
# Tool router: ONE dispatch point for MEMORY + MCP + plugin verbs.
# ---------------------------------------------------------------------------


def _plugin_verbs(cfg: Dict[str, Any]) -> List[str]:
    """All known plugin tool verbs (config + installed plugins).

    Assumes cfg is the merged session config. Never raises: discovery
    failures yield only the config-pinned verbs.
    """
    verbs: List[str] = []
    for key in ("plugin_tool_verbs", "agent_plugin_verbs"):
        try:
            vals = cfg.get(key) or []
            if isinstance(vals, str):
                vals = [vals]
            verbs.extend(str(v) for v in (vals or []) if isinstance(v, (str,)))
        except Exception:
            pass
    resolver = cfg.get("plugin_tool_verbs_resolver")
    if callable(resolver):
        try:
            discovered = resolver()
            if isinstance(discovered, str):
                discovered = [discovered]
            verbs.extend(str(value) for value in (discovered or []))
        except Exception:
            pass
    seen: List[str] = []
    for v in verbs:
        if v and v not in seen:
            seen.append(v)
    return seen


def route_tool(
    tool: str, args: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Classify one parsed tool call for the agent loop.

    Returns {"kind", ...} where kind is one of:
    - "builtin": read/glob/grep/bash/edit/write/memory/fetch/verify/done
    - "mcp": {"tool": "mcp"/"mcp_call"} — an installed MCP server call
    - "plugin": first token matches a known plugin verb
    - "unknown": anything else (the loop answers honestly, never crashes).
    Assumes tool is the lowercased parsed name. Never raises.
    """
    try:
        t = (tool or "").strip().lower()
        if t in (
            "read",
            "glob",
            "grep",
            "bash",
            "edit",
            "write",
            "memory",
            "fetch",
            "verify",
            "done",
        ):
            return {"kind": "builtin", "tool": t}
        if t in ("mcp", "mcp_call"):
            return {"kind": "mcp", "tool": t}
        verbs = _plugin_verbs(cfg or {})
        firsts = {str(v).split()[0].lower() for v in verbs if str(v).split()}
        if t in firsts:
            return {"kind": "plugin", "tool": t}
        return {"kind": "unknown", "tool": t}
    except Exception:
        return {"kind": "unknown", "tool": str(tool or "")}


def _resolve_mcp_server(label: str, cfg: Dict[str, Any]) -> Optional[str]:
    """Launch command for an MCP server label, or None when unknown.

    Assumes label is the plugin-manifest key (e.g. "structure-memory")
    or a connector label (`neo mcp add`). Sources: config
    agent_mcp_servers dict, then the unified connectors discovery
    (global < project < local < ... — cli.connectors, which itself
    folds in enabled plugins' manifests). Disabled plugins never
    resolve. Never raises.
    """
    try:
        cfg_map = cfg.get("agent_mcp_servers") or {}
        if isinstance(cfg_map, dict) and str(label) in cfg_map:
            return str(cfg_map[str(label)])
    except Exception:
        pass
    resolver = cfg.get("mcp_server_resolver")
    if callable(resolver):
        try:
            command = resolver(str(label))
            if command:
                return str(command)
        except Exception:
            pass
    return None


def _bash_escape_attempt(command: str) -> bool:
    """Return whether a live BASH command appears to write outside the repo."""
    text = str(command or "")
    lowered = text.lower()
    mutating = re.search(
        r"(?:^|[;&|]\s*|\s)(?:rm|mv|cp|touch|mkdir|rmdir|chmod|chown|tee|"
        r"echo|printf)\b|>>|(?<![<])>[^>]",
        lowered,
    )
    traversal = re.search(r"(?:^|[\s='\"])(?:\.\.[\\/])", text)
    absolute_write = re.search(
        r"(?:^|[\s='\"])(?:[a-zA-Z]:[\\/]|/)(?:etc|root|home|users|tmp|var|windows)[\\/]",
        text,
        re.IGNORECASE,
    )
    if traversal and mutating:
        return True
    if absolute_write and mutating:
        return True
    return bool(
        re.search(r"\bcd\s+(?:\.\.|/|[a-zA-Z]:[\\/]|~)(?:\s|$)", text, re.IGNORECASE)
    )


class _EventSink:
    """Adapt the loop's `_emit(kind, data)` onto the `trace.log(kind, data)`
    shape that `harness.tool_errors.ModelRecovery` expects.

    Exists so the recovery machinery writes into the SAME event stream the
    TUI consumes (ceiling invariant 12) instead of opening a second sink: a
    `model_recovery` record must be visible to any renderer of the journal.
    """

    __slots__ = ("_emit",)

    def __init__(self, emit: Any) -> None:
        self._emit = emit

    def log(self, kind: str, data: Dict[str, Any]) -> None:
        try:
            self._emit(kind, dict(data))
        except Exception:  # pragma: no cover — observability must not kill a run
            pass


def _new_cancel_token() -> Any:
    """A cancellation token for an in-flight tool call, best effort.

    Prefers `execution.workspace.CancellationToken` — the token type the real
    sandbox and the local execution handle both understand — and falls back to
    a local equivalent with the same `is_cancelled` / `cancel` surface. It
    must never raise: an uncancellable command is worse than a lost token, but
    a token failure must not be what kills a session.
    """
    try:
        from execution.workspace import CancellationToken

        return CancellationToken()
    except Exception:  # pragma: no cover — defensive
        import threading

        return threading.Event()


# The `malformed_tool_call` policy restates this shape plus ONE example,
# which is what actually lets a model self-correct a malformed call.
_AGENT_TOOL_SCHEMA_HINT = (
    'one JSON object per reply: {"tool": <name>, ...args} — names: '
    "READ(path) | GLOB(pattern) | GREP(query[,path]) | BASH(command) | "
    "EDIT(path, old, new) | WRITE(path, content) | MEMORY(query) | "
    "VERIFY() | DONE(answer). Paths are repo-relative; exactly one call "
    "per reply."
)

# AGT-02: the loop's own refusals, in its own words, mapped onto the kinds
# `harness.tool_errors` already defines. This is NOT a second classifier —
# it recognises the result strings THIS module produces, and the retry class
# is still derived from the kind by `tool_errors.retry_class_of`. The shapes
# it recognises are the ones `_execute_tool` returns:
#   "TOOL ERROR [<kind>]: ..."   the shared classifier's own render format
#   "... refused ..."            a harness/path/approval refusal -> command_rejected
#   "... miss / not found ..."  a path the model guessed wrong
#   "... needs ..."             a malformed argument list
#   "... syntax check failed"   a post-edit syntax gate
# Everything else degrades to `internal_error`, which is reflective (charged)
# rather than a refusal — an unrecognised failure must not silently cost a
# run its recovery budget.
_TOOL_ERROR_KIND_RE = re.compile(r"TOOL ERROR \[([a-z_]+)\]")


def _tool_result_kind(output: str) -> str:
    """The `harness.tool_errors` kind for one of this loop's tool results.

    Assumes `output` is the model-facing text `_execute_tool` returned.
    Never raises; an unrecognised shape is `internal_error`.
    """
    try:
        text = str(output or "")
        match = _TOOL_ERROR_KIND_RE.search(text)
        if match:
            return match.group(1)
        low = text.lower()
        if "rejected" in low or "refused" in low:
            return tool_errors.KIND_COMMAND_REJECTED
        if "syntax check failed" in low or "syntaxerror" in low:
            return tool_errors.KIND_SYNTAX_ERROR
        if " miss" in low or "not found" in low or "does not exist" in low:
            return tool_errors.KIND_FILE_NOT_FOUND
        if " needs " in low or low.startswith("needs "):
            return "argument_error"
        return tool_errors.KIND_INTERNAL_ERROR
    except Exception:  # pragma: no cover — defensive
        return tool_errors.KIND_INTERNAL_ERROR


def _run_bash_live(session: Any, command: str, cfg: Dict[str, Any]) -> str:
    """Run one bash command LIVE on the host (not Docker).

    Assumes session is a harness.tools.BashSession (deny-guard, cwd
    tracking, output caps stay). Uses an injected fake sandbox when
    tests set one (deps override wins); otherwise runs the local
    subprocess stub directly so Docker never sees a live-repo command.
    Raises PermissionError from the deny guard; KeyboardInterrupt
    propagates to the caller (which turns it into a tool result).

    A non-zero exit is NOT a failed call: the command RAN, it reported a
    non-zero status, and the result IS its output. Treating "exit 1" as a
    tool failure is what let this loop keep reflecting on a perfectly good
    test run, so the classification stays where the deny-guard and the
    sandbox put it (`BashSession.run`, on an EXCEPTION) and the historical
    `exit=N` shape is preserved byte-for-byte. A timeout IS an
    infrastructure limit rather than a result, so it is shaped for the model
    and the caller — not the model — decides what it means.
    """
    from harness import deps as deps_mod

    if _bash_escape_attempt(command):
        raise PermissionError("live BASH refused a path escape or outside-repo write")
    if getattr(deps_mod, "_execute_sandboxed_override", None) is not None:
        return session.run(command)
    from harness._stubs import sandbox as local_sandbox

    full = session._compose(command)
    # Deny-guard parity with BashSession.run (fail-loud, same shape).
    from harness.tools import _DENY_PAT

    if _DENY_PAT.search(command or ""):
        raise PermissionError(f"command denied by harness safety pattern: {command!r}")
    result = local_sandbox.execute_sandboxed(session.repo_path, full, session.timeout_s)
    session._track_cd(full)
    session._update_touched_files(command)
    session.commands.append(
        {
            "command": command,
            "exit_code": result.exit_code,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "timed_out": result.timed_out,
        }
    )
    if result.timed_out or int(result.exit_code or 0) == 124:
        return tool_errors.shape_tool_output(
            result.stdout or "",
            int(getattr(session, "max_output_chars", 3000) or 3000),
        )
    return session._map_result(result, command)


def render_agent_plan(
    request: str, repo_path: str = "", config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Render a lightweight agent plan preview (steps + files).

    Assumes request is the user's task text; repo_path may be "" (then
    files come from retrieval failure -> empty). The plan is a
    HEURISTIC proposal (explore -> change -> verify), never a verifier
    contract: no test names are fabricated, no success is claimed.
    An approved plan is injected as steering guidance, not enforced.
    Never raises.
    """
    try:
        cfg = get_config(config or {})
        files: List[str] = []
        try:
            from harness import retrieval as retrieval_mod

            ctx = retrieval_mod.retrieve_context(
                str(repo_path or "."),
                request,
                max_files=int(cfg.get("agent_context_files", 4)),
            )
            files = [str(f) for f in (ctx.get("files") or [])][:6]
        except Exception:
            files = []
        steps: List[str] = ["Explore the relevant code (READ/GREP first)"]
        if files:
            steps.append("Read: " + ", ".join(files[:4]))
        steps.append("Make the smallest change that satisfies the request")
        steps.append("Verify with BASH (run the relevant tests/command), then DONE")
        text = f"Task: {(request or '').strip()[:300]}\n"
        for i, s in enumerate(steps, start=1):
            text += f"{i}. {s}\n"
        if files:
            text += "Files likely involved: " + ", ".join(files) + "\n"
        return {"steps": steps, "files": files, "text": text}
    except Exception:
        return {
            "steps": ["Explore, change, verify"],
            "files": [],
            "text": str(request or "")[:300],
        }


def load_resume_history(task_id: str, log_root: Any, max_chars: int = 4000) -> str:
    """Rebuild prior-session context for resuming an agent task.

    Assumes task_id/log_root identify a previous run_agent session
    (its trace.jsonl holds task_start + tool_call/tool_result +
    edit_applied + result events). Returns a short human preamble:
    the original request, the files touched, and the last tool
    exchanges — never the full trace. Returns "" when there is
    nothing to replay (missing/unreadable trace). Never raises:
    a resume helper must never take down the resumed run.
    """
    try:
        tf = Path(log_root) / _safe_agent_id(task_id) / "trace.jsonl"
        if not tf.is_file():
            return ""
        request = ""
        answer = ""
        touched: List[str] = []
        exchanges: List[str] = []
        for ln in tf.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            kind = obj.get("kind")
            data = obj.get("data") or {}
            if kind == "task_start" and not request:
                request = str(data.get("issue_text") or "")[:500]
            elif kind == "tool_call":
                cmd = str(data.get("command") or data.get("tool") or "")[:160]
                if cmd:
                    exchanges.append(f"did: {cmd}")
            elif kind == "tool_result":
                out = str(data.get("output") or "")[:160].replace("\n", " ")
                if out:
                    exchanges.append(f"saw: {out}")
            elif kind == "edit_applied":
                p = str(data.get("path") or "")
                if p and p not in touched:
                    touched.append(p)
            elif kind == "result":
                answer = str(data.get("status") or "")
        bits: List[str] = []
        if request:
            bits.append(f"prior request: {request}")
        if touched:
            bits.append("files touched: " + ", ".join(touched[:8]))
        if exchanges:
            bits.append("recent activity:\n" + "\n".join(exchanges[-8:]))
        if answer:
            bits.append(f"prior outcome: {answer}")
        return "\n".join(bits)[: max(512, int(max_chars))]
    except Exception:
        return ""


def _session_context_receipt(value: Any) -> Tuple[str, Dict[str, Any]]:
    """Normalize an optional session context bundle for prompt injection."""
    if isinstance(value, Mapping):
        text = str(value.get("text") or value.get("context") or "").strip()
        sources = value.get("sources")
        if not isinstance(sources, list):
            sources = []
        included = [
            str(item.get("source"))
            for item in sources
            if isinstance(item, Mapping) and item.get("included") and item.get("source")
        ]
        omitted = [
            str(item.get("source"))
            for item in sources
            if isinstance(item, Mapping)
            and not item.get("included")
            and item.get("source")
        ]
    else:
        text = str(value or "").strip()
        included = []
        omitted = []
    text = text[:12000]
    return text, {
        "delivered": bool(text),
        "chars": len(text),
        "included_sources": included,
        "omitted_sources": omitted,
        "source_count": len(included) + len(omitted),
    }


#: Config key that pins the engine used when the caller names no strategy.
#: See `resolve_agent_dispatch`; the value is only consulted when it is
#: PRESENT and resolvable, so a `None` entry in `DEFAULTS` changes nothing.
AGENT_DEFAULT_STRATEGY_KEY = "agent_default_strategy"

#: Values of `AGENT_DEFAULT_STRATEGY_KEY` that mean "no override". They let a
#: settings file (TOML/JSON) turn the compatibility default OFF without
#: deleting the key, which a config chain that only merges cannot express.
_NO_COMPAT_DEFAULT = (None, "", "none", "default")


def resolve_agent_dispatch(
    config: Optional[Mapping[str, Any]] = None,
) -> Tuple[Optional[str], str, str]:
    """Decide, in ONE place, which engine a general-agent run is dispatched to.

    This is the single authority for the *dispatch* decision in this module.
    ``harness.agent_kernel.resolve_agent_strategy`` remains the single
    authority for the strategy *name* and its precedence; this function never
    re-implements that precedence, it calls it.

    Returns ``(dispatch, name, source)``:

    * ``dispatch`` is what is handed to ``SessionController.run_turn`` as its
      ``strategy`` argument, or ``None`` when this module has no opinion and
      the kernel resolver must decide. **Absence of a strategy is absence** --
      the historical behaviour of forcing ``explicit="legacy_agent"`` is what
      made the kernel's own ``daily`` default unreachable.
    * ``name`` is the strategy the run will actually use, from the resolver.
    * ``source`` says where the decision came from, and is reported on the run
      result as ``agent_strategy_source``: ``config`` (``agent_strategy`` was
      set), ``compat_default`` (nothing was named, and
      ``agent_default_strategy`` asked for the compatibility engine), or
      ``default`` (nothing was named anywhere, so the resolver's own default
      applies).

    Precedence, and it is the only precedence here: a strategy the caller
    NAMED always beats the compatibility DEFAULT, because a key whose name
    says "default" must not be able to overrule an actual choice. The
    resolver's own order is untouched -- this function only decides whether
    to name a strategy at all.

    Assumes ``config`` is a mapping of task config values; unknown keys are
    ignored. A ``compat_default`` value that is not a registered strategy
    raises ``ValueError`` (from the resolver) rather than silently falling
    back, so a typo cannot quietly restore the wrong engine.
    """
    values = config or {}
    named = values.get("agent_strategy")
    if named not in (None, ""):
        name, source = resolve_agent_strategy(values, spec=None)
        return None, str(name), str(source)
    compat = values.get(AGENT_DEFAULT_STRATEGY_KEY)
    if compat not in _NO_COMPAT_DEFAULT:
        name, _ = resolve_agent_strategy(values, explicit=compat, spec=None)
        return str(name), str(name), "compat_default"
    name, source = resolve_agent_strategy(values, spec=None)
    return None, str(name), str(source)


def run_agent_legacy(
    request: str,
    repo_path: str,
    config: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> AgentResult:
    """Run the general agent on the ``legacy_agent`` compatibility engine.

    This is the DEDICATED COMPATIBILITY ENTRY POINT: it forces
    ``agent_strategy="legacy_agent"`` so a caller that depended on the
    pre-0.3.0 default engine keeps working without editing its config, and a
    caller that wants the historical ``{"tool": ...}`` protocol can ask for
    it by name instead of by accident.

    Every keyword argument is forwarded verbatim to :func:`run_agent`, which
    owns the dispatch; this function adds nothing but the explicit strategy
    pin. The legacy path is fully supported -- it is a compatibility surface,
    not a fallback.
    """
    values = dict(config or {})
    values["agent_strategy"] = "legacy_agent"
    return run_agent(request, repo_path, values, **kwargs)


def run_agent(
    request: str,
    repo_path: str,
    config: Optional[Dict[str, Any]] = None,
    log_root: Optional[Path] = None,
    task_id: Optional[str] = None,
    approve_fn: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    plan_guidance: Optional[str] = None,
    resume_history: Optional[str] = None,
    session_context: Optional[Any] = None,
    session_id: Optional[str] = None,
) -> AgentResult:
    """Run the general agent for daily work through ``AgentKernel``.

    **The kernel is the single authority for which strategy runs.** This
    function does not override it: when the caller names no strategy, the
    absence is passed through as an absence (``strategy=None``) and
    ``harness.agent_kernel.resolve_agent_strategy`` decides, which means an
    unqualified run gets the resolver's own ``daily`` default. The single
    place that decision is made in this module is
    :func:`resolve_agent_dispatch`; nothing here re-derives the precedence.

    ``legacy_agent`` remains a fully supported compatibility surface, reached
    only by explicit request: ``agent_strategy="legacy_agent"``, the
    ``agent_default_strategy`` config key, or :func:`run_agent_legacy`.

    The resolved strategy and the source of that decision are reported on the
    result as ``agent_strategy`` / ``agent_strategy_source``; the kernel's
    ``run_started`` and ``strategy_selected`` journal rows carry the same
    pair, so a run's journal states which engine produced it.
    """

    if (config or {}).get("agent_kernel_enabled"):
        return run_agent_kernel(
            request=request,
            repo_path=repo_path,
            config=config,
            log_root=log_root,
            task_id=task_id,
            approve_fn=approve_fn,
            on_event=on_event,
            plan_guidance=plan_guidance,
            resume_history=resume_history,
            session_context=session_context,
            session_id=session_id,
        )
    if str((config or {}).get("agent_strategy") or "") == STEPPED_STRATEGY:
        return run_agent_stepped(
            request=request,
            repo_path=repo_path,
            config=config,
            log_root=log_root,
            task_id=task_id,
            approve_fn=approve_fn,
            on_event=on_event,
            resume_history=resume_history,
        )
    cfg = get_config(config or {})
    tid = _safe_agent_id(task_id or f"agent-{uuid.uuid4().hex[:8]}")
    dispatch, strategy, strategy_source = resolve_agent_dispatch(config)

    def emit(event: Dict[str, Any]) -> None:
        if on_event is None:
            return
        on_event({"kind": event.get("kind", ""), "data": event.get("data", {})})

    controller = SessionController(
        repo_path,
        log_root=log_root,
        config=cfg,
        on_event=emit,
        session_id=str(session_id or f"session-{tid}"),
    )
    result = controller.run_turn(
        request,
        run_id=tid,
        strategy=dispatch,
        resume=bool(resume_history),
        config=cfg,
        strategy_options={
            "plan_guidance": plan_guidance or "",
            "resume_history": resume_history or "",
            "session_context": session_context,
            "approval_callback": approve_fn,
        },
    )
    kernel = controller.kernels[tid]
    legacy = dict(kernel.last_legacy_result or {})
    legacy_status = str(legacy.get("status", "failed"))
    if strategy == "legacy_agent":
        # The compatibility adapter must NOT launder an unverified completion
        # into the historical `success` word: `completed_unverified` is
        # carried through verbatim, exactly as the strict adapter reports it.
        status = _compat_status(result.status, legacy_status)
    else:
        status = result.status
    output = AgentResult(
        {
            "mode": "agent",
            "status": status,
            "kernel_status": result.status,
            "agent_strategy": strategy,
            "agent_strategy_source": strategy_source,
            "agent_strategy_resolver": "harness.agent_loop.resolve_agent_dispatch",
            "answer": str(legacy.get("answer", result.answer)),
            "task_id": tid,
            "run_id": result.run_id,
            "session_id": result.session_id,
            "trace_path": result.trace_path,
            "checkpoint_path": result.checkpoint_path,
            "diff": str(legacy.get("diff", result.diff)),
            "files_touched": list(legacy.get("files_touched", result.changed_files)),
            "cost_usd": float(legacy.get("cost_usd", result.cost) or 0.0),
            "model_calls": list(legacy.get("model_calls", result.model_calls)),
        }
    )
    if legacy.get("verification") is not None:
        output["verification"] = legacy["verification"]
    # AGT-02: carry the reflection receipt (both caps, both counters, the
    # failure that stopped the loop) and the run's end reason through the
    # public adapter, so the CLI path can report the cap too. Additive.
    if legacy.get("reflection") is not None:
        output["reflection"] = legacy["reflection"]
    # AGT-10: same treatment for the queue audit, so a caller can learn that
    # a message it typed was queued and never delivered without parsing the
    # journal. Absent when nothing was ever queued.
    if legacy.get("steering_queue") is not None:
        output["steering_queue"] = legacy["steering_queue"]
    if legacy.get("end_reason"):
        output["end_reason"] = legacy["end_reason"]
    return output


def _compat_status(kernel_status: str, legacy_status: str) -> str:
    """Map a kernel status onto the historical agent status vocabulary.

    ``completed_unverified`` is preserved as its own value. It is the one
    status that must never collapse into ``success``: a model asking to stop
    without clean verifier evidence is not a success, and the historical
    ``success`` word is exactly what renderers treat as one.

    **The fall-through is a tightening, and it is why this function is pinned.**
    It used to end `return legacy_status or "failed"`, so any kernel status
    outside the closed `RUN_STATUSES` vocabulary returned the LEGACY status
    verbatim — and the legacy path's own status vocabulary contains the word
    ``success``. That is a laundering route: a run the kernel could not name
    would be reported to every renderer as a clean pass. `legacy.py` cannot
    produce an off-vocabulary status today, so this was a structural hole
    rather than a live defect; it is closed because a hole in a truthfulness
    path is worth closing even when nothing can walk through it yet.

    ``success`` is therefore returned from exactly ONE place: the
    ``completed_verified`` branch above. Everything else that claims to have
    finished lands on ``failed``.
    """
    if kernel_status == "completed_verified":
        return "success"
    if kernel_status == "completed_unverified":
        return "completed_unverified"
    if kernel_status == "timeout":
        return "timeout"
    if kernel_status == "cancelled":
        return "aborted"
    if kernel_status in {"failed", "blocked", "needs_input"}:
        return "failed" if legacy_status not in {"error", "aborted"} else legacy_status
    # Off-vocabulary kernel status: an unrecognised status is never a pass.
    # `error` and `aborted` are the two legacy values that are already honest
    # about what happened, so they may pass through; the historical `success`
    # word may not, because nothing here established that it was earned.
    if legacy_status in {"error", "aborted", "failed", "cancelled"}:
        return legacy_status
    return "failed"


def _run_agent_legacy(
    request: str,
    repo_path: str,
    config: Optional[Dict[str, Any]] = None,
    log_root: Optional[Path] = None,
    task_id: Optional[str] = None,
    approve_fn: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    plan_guidance: Optional[str] = None,
    resume_history: Optional[str] = None,
    session_context: Optional[Any] = None,
    session_id: Optional[str] = None,
    _trace: Optional[TraceLogger] = None,
) -> AgentResult:
    """Run one general agent task on the LIVE repo.

    Assumes repo_path is a readable directory (edited IN PLACE — this is
    the interactive daily-use path, not the benchmark path) and config is
    the session/task config dict (unknown keys pass through). Creates
    logs/{task_id}/ with trace.jsonl (the per-session record the TUI/REPL
    render), a pristine/ snapshot used ONLY as the diff/undo reference,
    and orig/ per-file originals for undo. plan_guidance, when given,
    is an approved lightweight plan injected as steering guidance (not
    a fixed contract — the loop may deviate). resume_history, when
    given, is PRIOR-session context (a resumed agent task's earlier
    turns) injected as a steering-context user message right after any
    plan guidance — history replay, not a restart: the loop starts
    from the current tree with the prior work visible. Returns an AgentResult dict:
    {status success|failed|error|aborted, answer, task_id, trace_path,
    diff, files_touched, cost_usd, model_calls, verification?}.
    Never mutates anything outside repo_path + its own log dir.
    """
    if (config or {}).get("agent_kernel_enabled"):
        return run_agent_kernel(
            request=request,
            repo_path=repo_path,
            config=config,
            log_root=log_root,
            task_id=task_id,
            approve_fn=approve_fn,
            on_event=on_event,
            plan_guidance=plan_guidance,
            resume_history=resume_history,
            session_context=session_context,
            session_id=session_id,
        )
    from harness import decision_memory, editor, retrieval
    from harness import skills as skills_mod

    cfg = get_config(config or {})
    tid = _safe_agent_id(task_id or f"agent-{uuid.uuid4().hex[:8]}")
    root = (
        Path(log_root).expanduser().resolve()
        if log_root
        else Path(cfg.get("work_subdir", "logs")).expanduser().resolve()
    )
    log_dir = root / tid
    log_dir.mkdir(parents=True, exist_ok=True)
    trace = _trace or TraceLogger(log_dir)
    model = ModelClient(trace, cfg)
    repo = Path(repo_path)

    def _emit(kind: str, data: Dict[str, Any]) -> None:
        trace.log(kind, data)
        if on_event is not None:
            try:
                on_event({"kind": kind, "data": data})
            except Exception:
                pass

    started = time.time()
    deadline = started + float(cfg.get("max_wallclock_s", 900.0))
    # P1/W1-T1: resolved through the ONE authority. The hard-coded 25
    # fallback that used to live here is DELETED rather than updated,
    # because a literal that exists is a literal that can drift again.
    # This is also the seam where a cap approach becomes an observation
    # (see `_announce_turn_cap`) instead of a silent stop.
    _caps = turn_caps.resolve_caps(cfg)
    max_turns = _caps.per_task
    approval_mode = str(
        cfg.get("agent_approval", cfg.get("approval", "auto")) or "auto"
    ).lower()

    if not (request or "").strip() or not repo.is_dir():
        _emit(
            "task_end", {"status": "error", "reason": "empty request or missing repo"}
        )
        return AgentResult(
            {
                "mode": "agent",
                "status": "error",
                "answer": "",
                "task_id": tid,
                "trace_path": str((log_dir / "trace.jsonl").resolve()),
                "diff": "",
                "files_touched": [],
                "cost_usd": 0.0,
                "model_calls": [],
            }
        )

    _emit(
        "task_start",
        {
            "task_id": tid,
            "mode": "agent",
            "repo_path": str(repo),
            "issue_text": request,
            "config": {k: v for k, v in cfg.items() if k != "api_key"},
        },
    )

    # Pristine snapshot: diff/undo reference ONLY (never built in).
    pristine = log_dir / "pristine"
    try:
        if not pristine.is_dir():
            editor.snapshot(str(repo), str(pristine))
    except OSError as exc:
        _emit("task_end", {"status": "error", "reason": f"snapshot failed: {exc}"})
        return AgentResult(
            {
                "mode": "agent",
                "status": "error",
                "answer": "",
                "task_id": tid,
                "trace_path": str((log_dir / "trace.jsonl").resolve()),
                "diff": "",
                "files_touched": [],
                "cost_usd": model.total_cost_usd,
                "model_calls": list(model.model_calls),
            }
        )

    steer = (
        steering_mod.SteeringBuffer(
            log_dir, tid, max_pending=int(cfg.get("max_pending_steering", 16))
        )
        if cfg.get("steering_enabled", True)
        else None
    )
    # AGT-10: the batch-boundary VIEW of that same journal. `steer` stays the
    # transport and the single consume authority; `queued` only records WHEN
    # an arrival was seen and WHEN it was handed over, so a message that is
    # queued and never delivered is a reported fact rather than a silent
    # hold. It is built even when steering is off (buffer=None) so the seam
    # below can be called unconditionally.
    queued = steering_mod.QueuedSteering(steer, task_id=tid)

    # Initial context: retrieval + memory, read-only over the live repo.
    try:
        ctx = retrieval.retrieve_context(
            str(repo),
            request,
            max_files=int(cfg.get("agent_context_files", 4)),
            index_root=root / "_code-graph",
        )
    except Exception:
        ctx = {"terms": [], "files": [], "greps": {}, "strategy": "none"}
    _emit(
        "retrieval",
        {
            "strategy": ctx.get("strategy"),
            "terms": ctx.get("terms", []),
            "files": ctx.get("files", []),
        },
    )
    skills_result = {
        "skills_block": "(none matched)",
        "matched": [],
        "considered": 0,
        "skipped": "skills_enabled=False"
        if not cfg.get("skills_enabled", True)
        else None,
        "error": None,
        "receipts": [],
        "rendered": [],
        "omitted": [],
    }
    if cfg.get("skills_enabled", True):
        try:
            skills_result = skills_mod.scan_skills_for_task(
                str(repo),
                request,
                retrieval_terms=ctx.get("terms", []),
                extra_roots=cfg.get("skills_roots"),
                max_skills=int(cfg.get("skills_max", 3)),
                max_chars=int(cfg.get("skills_max_chars", 2500)),
            )
        except Exception as exc:
            skills_result = {
                "skills_block": "(none matched)",
                "matched": [],
                "considered": 0,
                "skipped": "skill scan failed",
                "error": str(exc),
                "receipts": [],
                "rendered": [],
                "omitted": [],
            }
    skills_receipt = skills_mod.build_skill_receipt(
        skills_result,
        model_content=bool(skills_result.get("rendered"))
        and cfg.get("skills_enabled", True),
    )
    _emit("skills", dict(skills_receipt))
    _emit("skill_model_content", {**skills_receipt, "model_step": "agent-1"})
    skills_block = str(skills_result.get("skills_block") or "(none matched)")
    memory_block = "(none recorded yet)"
    if cfg.get("plan_with_memory", True):
        try:
            mem = decision_memory.query_planning_decisions(
                repo_path=str(repo),
                issue_text=request,
                retrieval_terms=ctx.get("terms", []),
                limit=int(cfg.get("memory_query_limit", 6)),
            )
            memory_block = decision_memory.render_memory_block(
                mem["decisions"], max_chars=int(cfg.get("memory_max_chars", 1500))
            )
            _emit(
                "decision_memory",
                {
                    "query": mem["query"],
                    "matched": len(mem["decisions"]),
                    "error": mem["error"],
                    "section_chars": len(memory_block),
                },
            )
        except Exception as exc:
            _emit("decision_memory", {"matched": 0, "error": str(exc)})
    else:
        _emit("decision_memory", {"matched": 0, "skipped": "plan_with_memory=False"})

    context_block = _context_block(
        str(repo),
        ctx.get("files", []) or [],
        int(cfg.get("agent_context_lines", 80)),
        int(cfg.get("agent_context_files", 4)),
    )
    session_text, session_receipt = _session_context_receipt(session_context)
    session_receipt["session_id"] = str(session_id or tid)
    _emit("session_context", session_receipt)
    session_section = f"## Session context\n{session_text}\n\n" if session_text else ""
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": AGENT_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## Task\n{request}\n\n"
                f"## Repo\n{repo}\n\n"
                f"## Retrieved context ({ctx.get('strategy', 'grep')})\n{context_block}\n\n"
                f"## Applicable skills\n{skills_block}\n\n"
                f"## Past decisions in this repo\n{memory_block}\n\n"
                f"{session_section}"
                "Begin. Reply with exactly ONE tool call."
            ),
        },
    ]
    if (plan_guidance or "").strip():
        guide_text = str(plan_guidance).strip()[:4000]
        messages.append(
            {
                "role": "user",
                "content": (
                    "## Approved plan (guidance, not a contract — deviate when the "
                    "repo shows a better path)\n"
                    + guide_text
                    + "\nContinue with one tool call."
                ),
            }
        )
        _emit(
            "steering",
            {"attempt": 0, "at": "agent-plan-approved", "texts": [guide_text]},
        )
    if (resume_history or "").strip():
        hist_text = str(resume_history).strip()[:4000]
        messages.append(
            {
                "role": "user",
                "content": (
                    "## Prior session context (resumed task — history replay, not "
                    "a restart; the tree already holds the earlier work)\n"
                    + hist_text
                    + "\nContinue with one tool call."
                ),
            }
        )
        _emit(
            "steering",
            {"attempt": 0, "at": "agent-resume-history", "texts": [hist_text]},
        )

    from harness import tools as tool_mod
    from harness.tools import BashSession

    # VEX-CEILING-07: a cancellation token makes an in-flight call
    # interruptible, so a hard steering abort terminates a long-running child
    # in seconds rather than after the whole command_timeout_s budget.
    session = BashSession(
        str(repo),
        int(cfg.get("command_timeout_s", 120)),
        int(cfg.get("max_output_chars", 3000)),
        cancellation_token=_new_cancel_token(),
    )

    # VEX-CEILING-07: ONE recovery policy and ONE bounded model-retry budget
    # for the whole session, so the forbidden-path set, the escalated timeout,
    # the loop guard and the turns-to-recovery statistic span the run.
    _recovery = tool_errors.recovery_policy_from_config(cfg, root=str(repo))
    _model_recovery = tool_errors.ModelRecovery(
        trace=_EventSink(_emit),
        max_attempts=int(cfg.get("max_model_attempts", 3)),
        base_backoff_s=float(cfg.get("model_retry_base_s", 0.5)),
        cap_backoff_s=float(cfg.get("model_retry_cap_s", 8.0)),
        label=tid,
    )
    fetch_budget = int(cfg.get("agent_max_fetches", 4))
    files_touched: List[str] = []
    protected = [str(p) for p in (cfg.get("protected_paths") or [])]
    verification: Optional[Dict[str, Any]] = None
    answer = ""
    status = "failed"
    turns_done = 0
    end_reason = ""

    # AGT-02: ONE bounded reflection loop for the whole run. Every failure the
    # loop sees is routed through `_reflect`, which decides (from the shared
    # `harness.tool_errors` classification) whether the failure is worth
    # another attempt, charges the budget when it is, and journals every one of
    # them. Nothing here decides success — the verifier gate below is untouched.
    _reflections = tool_errors.reflection_budget_from_config(
        cfg, trace=_EventSink(_emit), label=tid
    )

    def _reflect(
        kind: Any,
        *,
        evidence: str,
        signature: str = "",
        step_id: str = "",
        retry_class: Optional[str] = None,
        header: str = "",
    ) -> Tuple[bool, str]:
        """Turn ONE failure into the next user message, under the cap.

        Returns `(may_continue, text)`. `may_continue` is False ONLY when a
        cap bound the decision — that is the one case where the loop must stop
        and name the reason. A NON-RETRYABLE failure (a harness refusal, an
        environment fault, a terminal model failure) is reported and the loop
        continues, because the correct next move is a DIFFERENT one, not
        another attempt at this one; it simply spends no budget. `text` is
        always the rendered model-facing message, and the refused/exhausted
        shapes say so explicitly so a refusal can never read as an invitation
        to try again.
        """
        try:
            reflection = _reflections.note(
                kind,
                evidence=evidence,
                signature=signature,
                step_id=step_id,
                retry_class=retry_class,
            )
            text = tool_errors.render_reflection(reflection, header=header)
            return not bool(reflection.exhausted), text
        except Exception as exc:  # pragma: no cover — recovery never kills a run
            return True, (
                f"## REFLECTION [{exc.__class__.__name__}]\n"
                "The failure could not be classified; it is reported verbatim.\n\n"
                f"{str(evidence)[:2000]}"
            )

    def _exhaust(where: str) -> None:
        """Name the exhausted budget and the failure that exhausted it.

        A run that spent its reflections must say so WITH the last failure —
        never stop without a reason, and never stop silently.
        """
        nonlocal end_reason
        last = _reflections.last
        detail = (
            f"{last.kind} [{last.retry_class}]"
            if last is not None
            else "no recorded failure"
        )
        end_reason = (
            f"reflection budget exhausted at {where} after "
            f"{_reflections.run_used}/{_reflections.per_run} run reflections "
            f"(per-step cap {_reflections.per_step}); last failure: {detail}"
        )

    def _steering_dispatch(
        at: str, turn: int, slugs: Tuple[str, str, str]
    ) -> steering_mod.SteeringDelivery:
        """Consume pending steering and hand back its PARTITIONED delivery.

        ONE authority for the dispatch precedence (abort > replan > guide)
        in this loop, and it is deliberately shared by both consumers: the
        top-of-turn checkpoint and the AGT-10 batch-boundary seam. Two
        consumers that each re-derived the precedence are two answers to
        "which instruction wins", and a burst carrying an abort and a guide
        is exactly where they would disagree.

        `slugs` is `(abort, replan, guide)` — the `where` each consume is
        journalled under, passed in by the caller because the two seams name
        themselves differently and the slugs are part of the audit trail
        (tests and `shared.traceview` read them).

        Nothing is consumed that is not returned: `SteeringBuffer.take` is
        atomic, so a crash between here and the conversation append is
        visible as a delivery whose text never reached the model.
        """
        if steer is None:
            return steering_mod.SteeringDelivery(where=slugs[2], events=())
        _abort, _replan, _guide = steering_mod.partition_intents(steer.pending())
        if _abort:
            return queued.deliver(slugs[0], turn=turn)
        if _replan:
            return queued.deliver(slugs[1], turn=turn)
        return queued.deliver(slugs[2], turn=turn)

    def _steering_note(at: str, delivery: steering_mod.SteeringDelivery) -> None:
        """Journal ONE delivery: the event kind, the seam, and what it cost.

        Queueing and consumption are already in the steering journal; this is
        the loop-side receipt that names which seam moved them, so a reader
        can tell a mid-batch delivery from a turn-boundary one.
        """
        if delivery.empty:
            return
        if delivery.action() == steering_mod.INTENT_ABORT:
            _emit(
                "steering_abort",
                {"attempt": delivery.turn, "at": at, "texts": delivery.texts},
            )
            return
        if delivery.action() == steering_mod.INTENT_REPLAN:
            _emit(
                "steering_replan",
                {"attempt": delivery.turn, "at": at, "texts": delivery.texts},
            )
            return
        _emit(
            "steering",
            {
                "attempt": delivery.turn,
                "at": at,
                "seqs": delivery.seqs,
                "texts": delivery.texts,
                "waited_s": delivery.max_wait_s,
            },
        )

    def _needs_approval(tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
        if approval_mode != "require":
            return False
        if tool in ("bash", "edit", "write", "mcp", "mcp_call"):
            return True
        # Plugin verbs: read-only shapes run (auto, even in require
        # mode); anything else is rejected at execution — rejection, not
        # approval, is the guard (approval can never launder composition).
        try:
            kind = route_tool(tool, args or {}, cfg).get("kind")
            if kind == "plugin":
                return False
        except Exception:
            pass
        return False

    def _request_approval(tool: str, args: Dict[str, Any], preview: str) -> bool:
        _emit(
            "approval_required",
            {"tool": tool, "args": _jsonable(args), "preview": preview[:2000]},
        )
        if approve_fn is None:
            _emit(
                "approval_decided",
                {
                    "tool": tool,
                    "approved": False,
                    "reason": "no approver; agent_approval=require",
                },
            )
            return False
        try:
            ok = bool(approve_fn(tool, args, preview))
        except Exception as exc:
            _emit(
                "approval_decided",
                {"tool": tool, "approved": False, "reason": f"approver raised: {exc}"},
            )
            return False
        _emit("approval_decided", {"tool": tool, "approved": ok})
        return ok

    # AGT-10: a `break` inside a turn's tool batch becomes an entry here plus
    # a `return`, so the batch boundary is still reached exactly once and the
    # preserved result is still appended before the run ends. `""` means the
    # turn ran to completion.
    _batch_stop: List[str] = []
    # One observation per run, not one per turn: a run that approaches its cap
    # over twelve turns should say so once, not twelve times.
    _cap_warned = False

    for turn in range(1, max_turns + 1):
        turns_done = turn
        # P1/W1-T1: a cap this run is ABOUT to reach is a fact, so it is
        # reported. It is deliberately an OBSERVATION and not a stop: the
        # doom-loop guard already escalates to `needs_input` when it means a
        # decision is needed, and a turn cap that quietly truncated the run at
        # N-1 turns would be a second, invisible cap - the exact defect this
        # round exists to remove. The event is emitted ONCE per run (the
        # `warned` latch) so a long approach does not produce one row per turn.
        if not _cap_warned:
            _approach = turn_caps.approach_observation(turn, config=cfg)
            if _approach is not None:
                _cap_warned = True
                _emit("turn_cap_approaching", dict(_approach, caps=_caps.to_dict()))
        if time.time() >= deadline:
            status = "timeout"
            end_reason = "wall-clock limit"
            break
        if model.total_cost_usd >= float(cfg.get("budget_cap_usd", 2.0)):
            _emit("stop", {"reason": "budget cap", "usage": model.snapshot_usage()})
            status = "failed"
            end_reason = "budget cap exceeded"
            break

        # -- steering checkpoint (same journal as the fix loop) ---------
        # Turn boundary: the fallback seam. AGT-10 adds a SECOND consumer at
        # the tool-batch boundary, and both go through `_steering_dispatch`
        # so the intent precedence has exactly one definition.
        if steer is not None and steer.pending():
            _d = _steering_dispatch(
                "agent-turn",
                turn,
                ("abort-agent-turn", "replan-agent-turn", f"agent-turn-{turn}"),
            )
            if _d.action() == steering_mod.INTENT_ABORT:
                _steering_note("agent-turn", _d)
                answer = "aborted by user steering"
                status = "aborted"
                break
            if _d.action() == steering_mod.INTENT_REPLAN:
                _steering_note("agent-turn", _d)
                guide = "User steering (re-plan, keep work so far): " + " | ".join(
                    _d.texts
                )
                messages.append(
                    {
                        "role": "user",
                        "content": guide + "\nContinue with one tool call.",
                    }
                )
                continue
            _steering_note("agent-turn", _d)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "USER STEERING (incorporate and continue):\n"
                        + "\n".join(f"- {t}" for t in _d.texts)
                        + "\nContinue with exactly ONE tool call."
                    ),
                }
            )

        # VEX-CEILING-07 (G15): a transient provider failure is retried with
        # bounded backoff and emits `model_recovery`; only a TERMINAL failure
        # (auth / bad request / harness-internal) ends the run. The old shape
        # turned ONE provider hiccup into a task-ending error.
        try:
            _step_label = f"agent-{turn}"
            reply = _model_recovery.call(
                lambda s=_step_label: model.call(messages, step=s),
                step=_step_label,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _failure = _model_recovery.last_failure
            _kind = _failure.kind if _failure is not None else "model_internal"
            # AGT-02: a provider fault that survived `max_model_attempts` is
            # still TRANSIENT, so it is a reflection worth making — the run
            # gets another turn instead of dying. A TERMINAL failure (auth,
            # bad request, a harness bug) is not reflective and never charges
            # the budget: the run reports why it stopped, as it always did.
            _cls = tool_errors.retry_class_of_model_failure(
                _failure if _failure is not None else _kind
            )
            if _cls == tool_errors.RETRY_CLASS_TRANSIENT:
                _ok, _text = _reflect(
                    _kind,
                    evidence=(
                        f"the model provider failed this turn: "
                        f"{_failure.detail if _failure is not None else exc}"
                    ),
                    signature=f"model:{_kind}",
                    step_id="model-call",
                    retry_class=_cls,
                )
                if _ok:
                    messages.append({"role": "user", "content": _text})
                    _emit(
                        "tool_result",
                        {
                            "output": (
                                f"model call failed ({_kind}); reflected back to "
                                "the model"
                            ),
                            "ok": False,
                        },
                    )
                    continue
                _exhaust("the model call")
                messages.append({"role": "user", "content": _text})
            _emit(
                "model_failure_terminal",
                {
                    "kind": _kind,
                    "error": str(exc)[:500],
                    "attempts": _model_recovery.attempts,
                    "retry_class": _cls,
                },
            )
            _emit(
                "task_end",
                {"status": "error", "reason": f"model failed ({_kind}): {exc}"},
            )
            status = "error"
            end_reason = end_reason or f"terminal model failure ({_kind})"
            break

        call = parse_tool_call(reply, [v.split()[0] for v in _plugin_verbs(cfg)])
        if call is None:
            # `malformed_tool_call` policy: restate the shape and give ONE
            # worked example — a bare "not a valid tool call" does not teach.
            _malformed = _recovery.on_malformed_tool_call(
                "the reply was not a valid tool call",
                tool_schema=_AGENT_TOOL_SCHEMA_HINT,
                tool_example='{"tool": "READ", "path": "harness/core.py"}',
            )
            _emit(
                "tool_recovery",
                {
                    "kind": _malformed.kind,
                    "action": _malformed.action,
                    "count": _malformed.attempts,
                },
            )
            messages.append({"role": "assistant", "content": reply})
            # AGT-02: the failure (the unparseable reply itself) is the next
            # user message verbatim, the schema restatement is the evidence
            # attached to it, and the cap is what stops the loop asking again.
            _ok, _text = _reflect(
                _malformed.kind,
                evidence=f"{reply}\n\n{_recovery.feedback(_malformed)}",
                signature="parse",
                step_id="tool-parse",
            )
            if not _ok:
                _exhaust("the tool-call parse")
                messages.append({"role": "user", "content": _text})
                status = "failed"
                break
            messages.append({"role": "user", "content": _text})
            _emit(
                "tool_result",
                {"output": "unparseable tool call; asked to retry", "ok": False},
            )
            continue

        tool = call["tool"]
        args = {k: v for k, v in call.items() if k != "tool"}
        _emit(
            "tool_call",
            {
                "command": _render_tool_command(tool, args),
                "tool": tool,
                "args": _jsonable(args),
                "turn": turn,
            },
        )
        messages.append({"role": "assistant", "content": reply})

        if tool == "done":
            # AGT-10 — the final gate refuses a DONE that a QUEUED message
            # contradicts. This STRENGTHENS the gate; it is here because this
            # round added a delivery point, and a consume point is exactly
            # where a typed instruction could otherwise be swallowed by the
            # very turn that ends the run. A message typed during the model
            # call that produced this DONE has been pending since before the
            # top-of-turn checkpoint ran, so the pre-existing turn checkpoint
            # could not see it; without this the run would report success on
            # work the user had already told it to change.
            #
            # It costs a turn, and that is the price of not lying: the model
            # sees the correction and decides again. With steering off there
            # is no queue and no journal, so this is exactly one `if` on a
            # value that is empty.
            _final = _steering_dispatch(
                "final-gate",
                turn,
                (
                    "abort-agent-final",
                    "replan-agent-final",
                    f"agent-final-{turn}",
                ),
            )
            if not _final.empty:
                _steering_note("final-gate", _final)
                if _final.action() == steering_mod.INTENT_ABORT:
                    answer = "aborted by user steering before the final result"
                    status = "aborted"
                    end_reason = f"steering abort at {turn} (final gate)"
                    break
                _emit(
                    "steering_final_gate_deferred",
                    {
                        "turn": turn,
                        "at": "final-gate",
                        "texts": _final.texts,
                        "reason": (
                            "a queued instruction was still pending when the run "
                            "tried to finish; the result is deferred until it is "
                            "incorporated"
                        ),
                    },
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "USER STEERING arrived while you were finishing, so "
                            "this run is NOT over yet:\n"
                            + "\n".join(f"- {t}" for t in _final.texts)
                            + "\nIncorporate it, then reply with DONE and your "
                            "updated summary."
                        ),
                    }
                )
                continue
            answer = str(args.get("answer") or "").strip()
            # Verifier runs only when tests are declared.
            if cfg.get("target_test") or cfg.get("test_command"):
                verification = _run_verify(str(repo), cfg, trace, _emit)
                ok = bool(
                    verification.get("target_passed")
                    and verification.get("regression_passed")
                    and not verification.get("flaky")
                )
                status = "success" if ok else "failed"
            else:
                status = "success"
            break

        if _needs_approval(tool, args):
            preview = _approval_preview(str(repo), tool, args)
            if not _request_approval(tool, args, preview):
                msg = (
                    f"Approval required (agent_approval=require) for {tool.upper()}; "
                    "skipped — use READ/GREP/GLOB/MEMORY or ask the user."
                )
                _emit("tool_result", {"output": msg, "ok": False})
                # AGT-02: an approval refusal is a POLICY refusal. It is
                # journalled and it is FREE — it never spends a reflection, and
                # the rendered message says the decision stands rather than
                # inviting a rephrasing of the same call.
                _ok, _text = _reflect(
                    tool_errors.KIND_COMMAND_REJECTED,
                    evidence=msg,
                    signature=f"approval:{tool}",
                    step_id="approval",
                )
                messages.append({"role": "user", "content": _text})
                continue

        before_bash: set = set()
        if tool == "bash":
            try:
                before_bash = set(editor.changed_files(str(pristine), str(repo)))
            except Exception:
                before_bash = set()

        # -- TOOL BOUNDARY (G13/G14) ------------------------------------
        # Three behavioural checks BEFORE the call runs: a hard abort that
        # arrived while the model was thinking, a path the permission policy
        # already forbade, and a repeated identical call (doom-loop guard).
        if steer is not None and steer.has_intent("abort"):
            _emit(
                "steering_step_yield",
                {"turn": turn, "intent": "abort", "at": "pre-dispatch"},
            )
            # AGT-10: routed through the queue, like every other consume in
            # this loop, so a delivery is recorded wherever it happens. The
            # `consume` row is still written by `SteeringBuffer.take` inside
            # `QueuedSteering.deliver` — one consume authority, one journal —
            # but a receipt that only saw the SEAM's deliveries would
            # under-report a real one.
            taken = queued.deliver("abort-agent-dispatch", turn=turn)
            _emit(
                "steering_abort",
                {"turn": turn, "at": "tool-boundary", "texts": taken.texts},
            )
            status = "aborted"
            answer = "aborted by user steering before the pending tool call ran"
            end_reason = "steering abort at the tool boundary"
            break

        if tool == "bash":
            bash_command = str(args.get("command") or "")
            refusal = _recovery.rejects(bash_command)
            if refusal:
                _emit(
                    "command_refused",
                    {"turn": turn, "command": bash_command[:500], "reason": refusal},
                )
                _emit("tool_result", {"output": refusal, "ok": False})
                # AGT-02: a forbidden-path refusal is a POLICY refusal —
                # journalled, free, and phrased so it cannot be laundered into
                # "try the same command a different way".
                _ok, _text = _reflect(
                    tool_errors.KIND_PERMISSION_DENIED,
                    evidence=(
                        f"RECOVERY [permission_denied] -> forbid_path\n{refusal}"
                    ),
                    signature=bash_command,
                    step_id="bash-dispatch",
                )
                messages.append({"role": "user", "content": _text})
                continue
            loop_action = _recovery.note_command(bash_command)
            if loop_action is not None:
                blocked = _recovery.release_blocked_command()
                _emit(
                    "loop_guard",
                    {
                        "turn": turn,
                        "command": bash_command[:500],
                        "repeats": loop_action.attempts,
                        "action": loop_action.action,
                    },
                )
                _emit("tool_result", {"output": loop_action.evidence, "ok": False})
                # AGT-02: the doom-loop stop is journalled like every other
                # failure (class `terminal`, free — it is a harness decision,
                # not something another attempt can fix). The turn still ends,
                # with its own reason.
                _reflect(
                    tool_errors.KIND_LOOP_DETECTED,
                    evidence=_recovery.feedback(loop_action),
                    signature=blocked or bash_command,
                    step_id="bash-loop-guard",
                )
                messages.append(
                    {"role": "user", "content": _recovery.feedback(loop_action)}
                )
                status = "failed"
                end_reason = (
                    f"loop guard stopped a repeated identical command "
                    f"({loop_action.attempts}x): {(blocked or bash_command)[:200]}"
                )
                break

        # -- TOOL BATCH + THE BATCH BOUNDARY (AGT-10) ---------------------
        # A turn's tool batch runs through `harness.tools.run_tool_batch`, so
        # the safe seam BETWEEN completed calls is a first-class place rather
        # than a convention. The rule the primitive makes structural: a call
        # is never re-entered, split or interrupted; only a call that has
        # fully returned can be followed by a delivery. A `sleep 300` already
        # dispatched still runs to completion — but a correction typed while
        # it ran is now DELIVERED at the end of that turn instead of being
        # held for the next checkpoint.
        def _dispatch_one_tool(
            _turn: int,
            _tool: str,
            _args: Dict[str, Any],
            _before_bash: set,
        ) -> Tuple[bool, str]:
            """Execute one tool call and land its result in the conversation.

            A `break` in the old inline body became a `_batch_stop` entry
            plus a `return`, so the batch boundary is still reached exactly
            once and a preserved partial result is still appended BEFORE the
            run ends. That ordering is the point: the tool's output is never
            silently discarded because a correction arrived.
            """
            nonlocal status, answer, end_reason, verification, fetch_budget
            # -- IN-FLIGHT ABORT (G14) ----------------------------------
            # A hard abort that arrives while the call is running must
            # terminate the child process, not be noticed only after it
            # returns. The watcher polls the steering journal and cancels the
            # BashSession, whose token the real sandbox polls every 50ms
            # (killing the container) and the local handle acts on by killing
            # the tree. AGT-10's queue watcher runs beside it and does the
            # opposite job: it records that a message ARRIVED, and has no
            # hook at all, so it can never interrupt anything.
            _hard = steering_mod.HardAbortWatcher(steer, session.cancel).start()
            _queue_w = queued.watch()
            try:
                holder = {"fetch_left": [fetch_budget]}
                ok, output = _execute_tool(
                    str(repo),
                    _tool,
                    _args,
                    cfg,
                    session,
                    log_dir,
                    files_touched,
                    trace,
                    _emit,
                    messages,
                    holder,
                )
                try:
                    fetch_budget = int(holder["fetch_left"][0])
                except Exception:
                    pass
            except Exception as exc:
                ok, output = False, f"tool failed: {exc}"
            finally:
                _watcher_joined = _hard.stop()
                _queue_joined = _queue_w.stop()
            if _hard.errors:
                # A watcher that could not interrupt must say so: an abort
                # that silently failed to abort is worse than one that
                # reported it.
                _emit(
                    "steering_abort_watcher_error",
                    {
                        "turn": _turn,
                        "errors": _hard.errors[:4],
                        "aborted": _hard.aborted,
                    },
                )
            if _queue_w.errors:
                _emit(
                    "steering_queue_watcher_error",
                    {"turn": _turn, "errors": _queue_w.errors[:4]},
                )
            if _tool == "bash":
                try:
                    after_bash = set(editor.changed_files(str(pristine), str(repo)))
                    new_paths = sorted(after_bash - _before_bash)
                    for rel in new_paths:
                        _stash_pristine_original(log_dir, pristine, rel)
                        if rel not in files_touched:
                            files_touched.append(rel)
                    blocked = [
                        rel
                        for rel in new_paths
                        if editor.is_protected(rel, protected)
                        or Path(repo, rel).is_symlink()
                    ]
                    if blocked:
                        editor.restore_group(str(pristine), str(repo), blocked)
                        files_touched[:] = [
                            p for p in files_touched if p not in blocked
                        ]
                        ok = False
                        output = (
                            "BASH changed a protected or symbolic-link path: "
                            + ", ".join(blocked)
                        )
                except Exception as exc:
                    ok = False
                    output = f"BASH change audit failed: {exc}"

            # The in-flight RESULT is never silently discarded: it is emitted
            # and appended to the conversation BEFORE the hard abort ends the
            # run.
            if _hard.aborted:
                _emit(
                    "steering_abort_in_flight",
                    {
                        "turn": _turn,
                        "at": "tool-boundary",
                        "tool": _tool,
                        "elapsed_s": _hard.elapsed_to_abort_s(),
                        "result_preserved_chars": len(output or ""),
                        "watcher_joined": _watcher_joined,
                        "texts": _hard.abort_texts[:4],
                    },
                )
                _emit("tool_result", {"output": (output or "")[:4000], "ok": ok})
                # AGT-10: also through the queue, for the same reason as the
                # pre-dispatch abort — a consume the receipt cannot see is a
                # consume the audit would have to guess about.
                _taken = (
                    queued.deliver("abort-agent-inflight", turn=_turn)
                    if steer is not None
                    else steering_mod.SteeringDelivery(
                        where="abort-agent-inflight", events=()
                    )
                )
                _emit(
                    "steering_abort",
                    {
                        "turn": _turn,
                        "at": "tool-boundary-inflight",
                        "texts": _taken.texts or _hard.abort_texts[:4],
                    },
                )
                status = "aborted"
                answer = (
                    "aborted by user steering while a tool call was running; its "
                    "partial result was preserved and the session stays resumable"
                )
                end_reason = f"steering abort at {_turn} (in-flight)"
                _batch_stop.append("in_flight_abort")
                return ok, output

            # VEX-CEILING-07 (G13): a FAILED call selects a recovery ACTION
            # from the per-kind policy, so the loop's NEXT move differs by
            # error kind (narrow + longer budget / attach a listing / forbid a
            # path / attach the numbered file) instead of the same generic
            # "try again".
            _action = None
            if not ok and _tool == "bash" and getattr(session, "last_error", None):
                _action = _recovery.on_tool_error(
                    session.last_error,
                    command=str(_args.get("command") or ""),
                    files_touched=files_touched,
                )
                if _action.timeout_s:
                    session.set_timeout(_action.timeout_s)
                _emit(
                    "command_recovery",
                    {
                        "turn": _turn,
                        "command": str(_args.get("command") or "")[:500],
                        "recovery": _action.to_dict(),
                    },
                )
            _emit("tool_result", {"output": output[:4000], "ok": ok})
            if ok:
                # AGT-02: a productive turn ends the current failure streak,
                # so the per-step cap bounds CONSECUTIVE failures rather than
                # the whole run. The per-run cap is the lifetime bound.
                _reflections.note_success()
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"## Tool result ({_tool}, ok)\n{output[:4000]}\n\n"
                            "Continue with exactly ONE tool call, or DONE with "
                            "your summary."
                        ),
                    }
                )
            else:
                # AGT-02: the failure IS the next user message — the tool's
                # own output verbatim, with the per-kind evidence the recovery
                # policy attached, classified through the ONE shared table so
                # a refusal costs no budget and a transient/task failure is
                # charged.
                _kind = (
                    _action.kind
                    if _action is not None
                    else (
                        tool_errors.KIND_VERIFICATION_FAILED
                        if _tool == "verify"
                        else _tool_result_kind(output)
                    )
                )
                _ok, _text = _reflect(
                    _kind,
                    evidence=(
                        f"## Tool result ({_tool}, error)\n{output[:4000]}\n\n"
                        + (_recovery.feedback(_action) if _action is not None else "")
                    ),
                    signature=(f"{_tool}:{_render_tool_command(_tool, _args)}"[:200]),
                    step_id=f"tool:{_tool}",
                )
                if not _ok:
                    # The budget is spent: say so WITH the last failure
                    # instead of spending another turn, and never call it a
                    # success.
                    _exhaust(f"the {_tool} call")
                    messages.append({"role": "user", "content": _text})
                    status = "failed"
                    _batch_stop.append("reflection_exhausted")
                else:
                    messages.append({"role": "user", "content": _text})
            return ok, output

        def _deliver_at_batch_boundary(_turn: int, _tool: str) -> bool:
            """THE SEAM (AGT-10). Deliver what was typed while the batch ran.

            Runs AFTER a call has fully returned, which is the whole safety
            argument: a mutating call is never interrupted to make room for a
            message. Returns False to stop the batch, which happens only for
            a hard abort delivered here.

            A run that is already ending (`_batch_stop`) does NOT deliver:
            the events stay PENDING so the resume contract still applies
            them, and `receipt()["undelivered"]` names them, because a
            message the user typed must never be quietly swallowed by a
            terminal turn.
            """
            nonlocal status, answer, end_reason
            # The OFF arm is one code path, not a special case that stops the
            # run: with steering off there is no queue to deliver, so the
            # seam has nothing to do and says "carry on".
            if steer is None:
                return True
            if _batch_stop:
                return False
            _bd = _steering_dispatch(
                f"agent-batch-{_turn}",
                _turn,
                (
                    "abort-agent-batch",
                    "replan-agent-batch",
                    f"agent-batch-{_turn}",
                ),
            )
            if _bd.empty:
                return True
            _emit(
                "steering_batch_boundary",
                {
                    "turn": _turn,
                    "at": f"agent-batch-{_turn}",
                    "tool": _tool,
                    **{
                        k: _bd.as_dict()[k]
                        for k in ("action", "seqs", "intents", "texts", "max_wait_s")
                    },
                },
            )
            _steering_note(f"agent-batch-{_turn}", _bd)
            if _bd.action() == steering_mod.INTENT_ABORT:
                status = "aborted"
                answer = (
                    "aborted by user steering at the tool-batch boundary; the "
                    "completed call's result was preserved and the session "
                    "stays resumable"
                )
                end_reason = f"steering abort at {_turn} (batch boundary)"
                _batch_stop.append("abort_at_batch_boundary")
                return False
            if _bd.action() == steering_mod.INTENT_REPLAN:
                # A replan does NOT burn a turn here. The turn boundary
                # `continue`s past the model call; at a seam the tool result
                # is already in the conversation, so the guidance rides the
                # next model call instead of costing an empty one.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "USER STEERING (re-plan, keep work so far): "
                            + " | ".join(_bd.texts)
                            + "\nThe tool result above stands. Continue with "
                            "exactly ONE tool call."
                        ),
                    }
                )
                return True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "USER STEERING (queued while that tool ran — "
                        "incorporate and continue):\n"
                        + "\n".join(f"- {t}" for t in _bd.texts)
                        + "\nContinue with exactly ONE tool call."
                    ),
                }
            )
            return True

        # The turn's tool batch: ONE call (this loop's protocol is one call
        # per reply), carried with everything the dispatch needs so the
        # dispatch and the seam are bound to THIS turn's values rather than
        # to the loop variable by closure. `harness.tools.run_tool_batch` is
        # the shared primitive, so a dispatcher that really does run N calls
        # per turn gets N seams from the same code.
        _batch = tool_mod.run_tool_batch(
            [(turn, tool, args, before_bash)],
            lambda item: _dispatch_one_tool(item[0], item[1], item[2], item[3]),
            seam=lambda _b, _t=turn, _k=tool: _deliver_at_batch_boundary(_t, _k),
        )
        if _batch_stop or _batch.stopped:
            break
        if tool == "verify":
            # Keep the last explicit VERIFY verdict for the final result.
            try:
                for ev in reversed(trace.read_all()):
                    if ev.get("kind") == "verify":
                        d = ev.get("data") or {}
                        verification = {
                            "target_passed": bool(d.get("target_passed")),
                            "regression_passed": bool(d.get("regression_passed", True)),
                            "flaky": bool(d.get("flaky")),
                            "raw": str(d.get("raw") or ""),
                        }
                        break
            except Exception:
                pass

    else:
        end_reason = "max turns exhausted"

    # -- close -------------------------------------------------------------
    try:
        diff = editor.unified_diff(str(pristine), str(repo)) or ""
    except Exception:
        diff = ""
    # VEX-CEILING-07: the session's recovery receipt (per-kind occurrences,
    # recoveries, mean turns-to-recovery, forbidden paths, model retries)
    # lands in the SAME event stream before task_end, so any renderer of the
    # journal sees why a run took the turns it took. AGT-02 adds the
    # reflection budget beside them: both caps, both counters, and which cap
    # bound the last decision.
    _emit(
        "recovery_stats",
        {
            "policy": _recovery.stats(),
            "model": _model_recovery.report(),
            "reflection": _reflections.report(),
        },
    )
    # AGT-10: the queue's audit closes with the run. `undelivered` is the
    # load-bearing key — a non-empty list means something the user typed was
    # queued and never handed to the model, which the run says out loud
    # rather than reporting a clean finish over it.
    _queue_receipt = queued.receipt()
    if _queue_receipt["queued"] or _queue_receipt["deliveries"]:
        _emit("steering_queue", _queue_receipt)
    if status in ("success", "failed", "timeout") and status != "aborted":
        payload = {"status": status, "turns": turns_done}
        if end_reason:
            payload["reason"] = end_reason
        _emit("task_end", payload)
    elif status == "aborted":
        _emit("task_end", {"status": "failed", "aborted": True})
        status = "failed"
    result_payload: Dict[str, Any] = {
        "status": status,
        "attempts": 1,
        "cost_usd": round(model.total_cost_usd, 6),
        "task_id": tid,
    }
    _emit("result", result_payload)
    out = AgentResult(
        {
            "mode": "agent",
            "status": status,
            "answer": answer,
            "task_id": tid,
            "trace_path": str((log_dir / "trace.jsonl").resolve()),
            "diff": diff,
            "files_touched": sorted(set(files_touched)),
            "cost_usd": model.total_cost_usd,
            "model_calls": list(model.model_calls),
        }
    )
    if verification is not None:
        out["verification"] = verification
    # AGT-02: the reflection budget is part of the run's receipt, not just a
    # journal row — both caps, both counters, and the failure that stopped the
    # loop. Additive: every existing key keeps its meaning.
    out["reflection"] = _reflections.report()
    # AGT-10: the queue's audit is on the result too, so a caller does not
    # have to parse the journal to learn that a message it typed was queued
    # and never delivered (`undelivered` non-empty). Additive.
    if _queue_receipt["queued"] or _queue_receipt["deliveries"]:
        out["steering_queue"] = _queue_receipt
    if end_reason:
        out["end_reason"] = end_reason
    return out


def run_agent_kernel(
    request: str,
    repo_path: str,
    config: Optional[Dict[str, Any]] = None,
    log_root: Optional[Path] = None,
    task_id: Optional[str] = None,
    approve_fn: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    plan_guidance: Optional[str] = None,
    resume_history: Optional[str] = None,
    session_context: Optional[Any] = None,
    session_id: Optional[str] = None,
) -> AgentResult:
    """Run the strict shared-kernel daily strategy.

    This is the migration seam for callers that can consume the honest
    ``completed_verified``/``completed_unverified`` statuses. The historical
    :func:`run_agent` remains available for CLI compatibility until its
    callers adopt this result contract.
    """
    from harness import skills as skills_mod
    from harness.agent_kernel import AgentKernel, RunSpec

    cfg = get_config(config or {})
    tid = _safe_agent_id(task_id or f"agent-{uuid.uuid4().hex[:8]}")
    policy = {
        key: cfg.get(key)
        for key in (
            "target_test",
            "test_command",
            "baseline_reruns",
            "verify_timeout_s",
        )
        if key in cfg
    }
    metadata = _json_safe_config(cfg)
    skill_result: Dict[str, Any] = {
        "skills_block": "(none matched)",
        "matched": [],
        "considered": 0,
        "receipts": [],
        "rendered": [],
        "omitted": [],
        "skipped": "skills_enabled=False"
        if not cfg.get("skills_enabled", True)
        else None,
        "error": None,
    }
    if cfg.get("skills_enabled", True):
        try:
            skill_result = skills_mod.scan_skills_for_task(
                repo_path,
                request,
                extra_roots=cfg.get("skills_roots"),
                max_skills=int(cfg.get("skills_max", 3)),
                max_chars=int(cfg.get("skills_max_chars", 2500)),
            )
        except Exception as exc:
            skill_result = {
                "skills_block": "(none matched)",
                "matched": [],
                "considered": 0,
                "receipts": [],
                "rendered": [],
                "omitted": [],
                "skipped": "skill scan failed",
                "error": str(exc),
            }
    metadata["skills_block"] = str(skill_result.get("skills_block") or "(none matched)")
    metadata["skills_receipt"] = skills_mod.build_skill_receipt(
        skill_result,
        model_content=bool(skill_result.get("rendered"))
        and cfg.get("skills_enabled", True),
    )
    if plan_guidance:
        metadata["plan_guidance"] = str(plan_guidance)[:4000]
    if resume_history:
        metadata["resume_history"] = str(resume_history)[:4000]
    session_text, session_receipt = _session_context_receipt(session_context)
    metadata["session_context"] = session_text
    metadata["session_context_receipt"] = session_receipt
    spec = RunSpec(
        session_id=str(session_id or f"session-{tid}"),
        run_id=tid,
        request=request,
        repository_identity=repo_path,
        strategy="daily",
        verification_policy=policy,
        metadata=metadata,
    )
    events_seen: List[Dict[str, Any]] = []

    def _event(event: Dict[str, Any]) -> None:
        events_seen.append(event)
        if on_event is not None:
            on_event(event)

    kernel = AgentKernel(
        repo_path=repo_path,
        log_root=log_root,
        config=cfg,
        approval_callback=approve_fn,
        on_event=_event,
    )
    result = kernel.run(spec, strategy="daily", resume=bool(resume_history))
    verification = None
    if result.verification_evidence:
        verification = result.verification_evidence[-1]
    output = AgentResult(
        {
            "mode": "agent",
            "status": result.status,
            "kernel_status": result.status,
            "answer": result.answer,
            "task_id": tid,
            "run_id": result.run_id,
            "session_id": result.session_id,
            "trace_path": result.trace_path,
            "checkpoint_path": result.checkpoint_path,
            "diff": result.diff,
            "files_touched": list(result.changed_files),
            "cost_usd": result.cost,
            "model_calls": list(result.model_calls),
            "resume_availability": result.resume_availability,
            "follow_up_needs": list(result.follow_up_needs),
            "events": events_seen,
        }
    )
    if verification is not None:
        output["verification"] = verification
    return output


# ---------------------------------------------------------------------------
# AGT-11: the adapter for the pure core.
#
# Everything below this line is an ADAPTER. It owns no decision: which turn to
# take, whether a call is refused, what a failure means, and above all whether
# a run completed are all made by `harness.agent_loop_step.step`, whose only
# inputs are a history, an injected boundary and a config. This module supplies
# the effects -- a real model, a real sandbox, a real verifier, a real clock,
# a real steering journal -- and appends the resulting events to the SAME
# append-only `logs/{task_id}/trace.jsonl` that was always the durability
# boundary. Persistence is not rebuilt; it is written to, exactly as before.
# ---------------------------------------------------------------------------


class HarnessToolbox(step_mod.LoopEnvironment):
    """The production injected boundary for :func:`harness.agent_loop_step.step`.

    This class is the whole of the "side effects" half of the loop. It owns the
    four capabilities the pure core deliberately does not have -- the model, the
    tools, the clock, the meter -- plus the approver and the steering inbox.

    It holds no decision logic. In particular it does not decide whether a run
    completed, whether a call was allowed, or how a failure should be
    classified: those belong to the core, and a decision smuggled in here would
    be a decision the tests cannot reach.
    """

    def __init__(
        self,
        repo: str,
        cfg: Dict[str, Any],
        model: ModelClient,
        session: Any,
        log_dir: Path,
        files_touched: List[str],
        trace: TraceLogger,
        emit: Callable[[str, Dict[str, Any]], None],
        *,
        started: float,
        approve_fn: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
        steering_queue: Optional[Any] = None,
        fetch_budget: Optional[List[int]] = None,
        messages: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        self.repo = str(repo)
        self.cfg = cfg
        self.model = model
        self.session = session
        self.log_dir = Path(log_dir)
        self.files_touched = files_touched
        self.trace = trace
        self.emit = emit
        self.started = float(started)
        self.approve_fn = approve_fn
        self.steering_queue = steering_queue
        self.fetch_left = fetch_budget if fetch_budget is not None else [0]
        self.messages = messages if messages is not None else []

    # -- the two required capabilities -------------------------------------

    def ask(self, messages: Any, *, step: str = "") -> str:
        """One model call through the configured Boundary-2 client."""
        self.messages = list(messages or [])
        return self.model.call(self.messages, step=str(step or "agent-1"))

    def invoke(self, name: str, args: Any, *, turn: int = 0) -> step_mod.ToolOutcome:
        """Run ONE tool, or the verifier, and return a shaped outcome.

        `_execute_tool` and `_run_verify` are the SAME functions the legacy
        engine uses, so a tool behaves identically on both paths and there is
        no second executor to drift. The only work here is shaping the
        (ok, output) pair the legacy dispatcher already returns into the
        structured outcome the pure core reasons about.
        """
        mapping = dict(args or {})
        if name == step_mod.VERIFIER_TOOL:
            # `_run_verify` is asked for a NO-OP emit on purpose. It emits its
            # own `verify` row for the legacy loop's benefit, and the pure core
            # emits the canonical one from the same evidence; letting both land
            # would put two rows for one verifier run in the authoritative
            # journal, and a reader counting verifier runs would be right to
            # distrust the number.
            evidence = _run_verify(self.repo, self.cfg, self.trace, lambda *_a: None)
            clean = _evidence_is_clean(evidence)
            return step_mod.ToolOutcome(
                ok=clean,
                output=str(evidence.get("raw") or ""),
                kind="" if clean else tool_errors.KIND_VERIFICATION_FAILED,
                detail=evidence,
            )
        before = list(self.files_touched)
        budgets = {"fetch_left": self.fetch_left}
        try:
            ok, output = _execute_tool(
                self.repo,
                name,
                mapping,
                self.cfg,
                self.session,
                self.log_dir,
                self.files_touched,
                self.trace,
                self.emit,
                self.messages,
                budgets,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # pragma: no cover — the core also guards this
            return step_mod.ToolOutcome(
                ok=False,
                output=f"tool failed: {exc}",
                kind=tool_errors.KIND_INTERNAL_ERROR,
            )
        # The executor is the only thing that knows what it actually wrote, so
        # the paths it changed are attached here rather than re-derived by the
        # core — otherwise the terminal receipt's `files_touched` would be
        # empty on the real path while the result carried the list.
        touched = [p for p in self.files_touched if p not in before]
        detail: Dict[str, Any] = {"path": touched[0]} if touched else {}
        if touched:
            detail["paths"] = touched
        return step_mod.ToolOutcome(
            ok=bool(ok),
            output=str(output or ""),
            kind="" if ok else (tool_errors.error_kind_of({"output": output}) or ""),
            detail=detail,
        )

    # -- the injected environment readings ---------------------------------

    def elapsed_s(self) -> float:
        """Seconds since this run started. The ONLY place in the pure-step path
        that reads a clock, and it is here on purpose."""
        return max(0.0, time.time() - self.started)

    def spent_usd(self) -> float:
        """Spend so far, from the same client the legacy engine meters."""
        return float(getattr(self.model, "total_cost_usd", 0.0) or 0.0)

    def cancelled(self) -> bool:
        """Whether this run's cancellation token has been tripped.

        The steering abort watcher is what trips it, so this is the same signal
        a real hard abort uses rather than a parallel cancel channel.
        """
        return bool(getattr(self.session, "cancelled", False))

    def approve(self, name: str, args: Any, *, turn: int = 0) -> bool:
        """The human's decision, or an honest refusal when there is no human.

        `approve_fn is None` means no approver is reachable, and the answer is
        NO. The legacy loop reaches the same conclusion; keeping it is what
        makes an unattended `require`-mode run fail closed instead of
        running an edit nobody saw.
        """
        if self.approve_fn is None:
            return False
        return bool(
            self.approve_fn(
                str(name),
                dict(args or {}),
                _approval_preview(self.repo, str(name), dict(args or {})),
            )
        )

    def steering(self, where: str, *, turn: int, tool: str = "") -> Any:
        """Consume pending steering at the core's turn checkpoint."""
        if self.steering_queue is None:
            return None
        try:
            return self.steering_queue.deliver(
                f"{where}-{turn}", turn=turn, tool=str(tool or "")
            )
        except Exception:  # pragma: no cover — a journal we cannot read is empty
            return None


def _evidence_is_clean(evidence: Mapping[str, Any]) -> bool:
    """Whether verifier evidence proves a clean target AND regression pass.

    The same three-term conjunction, and for the same reason as in the pure
    core: an absent term is False, never a default. This is the adapter's copy
    because the adapter has to answer the question before the core asks it, and
    two copies of a mint condition are a hazard -- so both are pinned equal to
    each other and to `harness/agent_kernel/legacy.py`'s in
    `tests/test_agent_loop_matrix.py`.
    """
    if not evidence or evidence.get("error"):
        return False
    return bool(
        evidence.get("target_passed")
        and evidence.get("regression_passed")
        and not evidence.get("flaky")
    )


def run_agent_stepped(
    request: str,
    repo_path: str,
    config: Optional[Dict[str, Any]] = None,
    log_root: Optional[Path] = None,
    task_id: Optional[str] = None,
    approve_fn: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    resume_history: Optional[str] = None,
) -> AgentResult:
    """Run the general agent on the PURE-STEP engine (``agent_step``).

    This is the adapter around :func:`harness.agent_loop_step.step`, and it is
    deliberately thin. It:

    1. resolves the log directory and opens the append-only ``trace.jsonl`` --
       the durability boundary, unchanged and not rebuilt;
    2. snapshots the pristine tree and seeds the conversation with the system
       prompt, the task, retrieval/skills/memory context, an approved plan, and
       (on a resume) the replayed history;
    3. builds a :class:`HarnessToolbox` around the real model, the real
       executor, the real verifier, a real clock and the real steering journal;
    4. calls ``step`` ONCE and appends every returned event to the trace, in
       order, plus the same ``on_event`` callback the legacy path uses;
    5. returns an :class:`AgentResult` whose ``status`` is the core's terminal
       status VERBATIM.

    Point 5 is the honesty boundary. ``completed_verified`` requires a
    declared verifier that returned clean evidence; a run with no declared
    tests is ``completed_unverified``; and there is no configuration of this
    function that turns either into the bare word the legacy engine still
    mints. The verifier gate is not this adapter's to weaken, and this adapter
    does not have the vocabulary to try.
    """
    cfg = get_config(config or {})
    tid = _safe_agent_id(task_id or f"agent-{uuid.uuid4().hex[:8]}")
    repo = Path(str(repo_path))
    root = Path(log_root) if log_root else repo / str(cfg.get("work_subdir", "logs"))
    log_dir = root / tid
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return AgentResult(
            {
                "mode": "agent",
                "status": "error",
                "kernel_status": "error",
                "agent_strategy": STEPPED_STRATEGY,
                "agent_strategy_source": "config",
                "agent_strategy_resolver": "harness.agent_loop.run_agent_stepped",
                "answer": f"could not create the log directory: {exc}",
                "task_id": tid,
                "files_touched": [],
                "cost_usd": 0.0,
            }
        )
    trace = TraceLogger(log_dir)

    def emit(kind: str, data: Dict[str, Any]) -> None:
        """Write one row to the authoritative trace and forward it.

        The trace write is the durability boundary and is the same one the
        legacy loop writes to; the callback is best-effort because a renderer
        that raises must not change a run's outcome.
        """
        trace.log(kind, data)
        if on_event is None:
            return
        try:
            on_event({"kind": kind, "data": dict(data)})
        except Exception:  # pragma: no cover — observability never kills a run
            pass

    if not str(request or "").strip() or not repo.is_dir():
        emit(
            "task_end",
            {
                "status": "error",
                "reason": "an empty request or a non-directory repository",
            },
        )
        return AgentResult(
            {
                "mode": "agent",
                "status": "error",
                "kernel_status": "error",
                "agent_strategy": STEPPED_STRATEGY,
                "agent_strategy_source": "config",
                "agent_strategy_resolver": "harness.agent_loop.run_agent_stepped",
                "answer": "",
                "task_id": tid,
                "files_touched": [],
                "cost_usd": 0.0,
            }
        )

    emit(
        "task_start",
        {
            "task_id": tid,
            "mode": "agent",
            "engine": STEPPED_STRATEGY,
            "repo_path": str(repo),
            "issue_text": str(request),
            "config": _json_safe_config(cfg),
        },
    )

    pristine = log_dir / "pristine"
    try:
        from harness import editor as editor_mod

        editor_mod.snapshot(str(repo), str(pristine))
    except Exception as exc:
        emit("task_end", {"status": "error", "reason": f"snapshot failed: {exc}"})
        return AgentResult(
            {
                "mode": "agent",
                "status": "error",
                "kernel_status": "error",
                "agent_strategy": STEPPED_STRATEGY,
                "agent_strategy_source": "config",
                "agent_strategy_resolver": "harness.agent_loop.run_agent_stepped",
                "answer": "",
                "task_id": tid,
                "files_touched": [],
                "cost_usd": 0.0,
            }
        )

    from harness import decision_memory as memory_mod
    from harness import editor as editor_mod
    from harness import retrieval as retrieval_mod
    from harness import skills as skills_mod
    from harness.tools import BashSession

    model = ModelClient(trace, cfg)
    started = time.time()
    files_touched: List[str] = []

    # -- the seeded conversation -------------------------------------------
    # Every block below is a CONTEXT block appended to one list. None of them
    # decides what the run is allowed to do; that is the core's job. The seed
    # SHAPE is the legacy loop's, deliberately: the two engines must see the
    # same first prompt or a comparison between them means nothing.
    strategy = "grep"
    context_block = ""
    try:
        ctx = retrieval_mod.retrieve_context(
            str(repo), str(request), index_root=root / "_code-graph"
        )
        strategy = str(ctx.get("strategy", "grep"))
        context_block = _context_block(
            str(repo),
            list(ctx.get("files") or []),
            int(cfg.get("agent_context_lines", 120)),
            int(cfg.get("agent_context_files", 6)),
        )
        emit(
            "retrieval",
            {
                "strategy": strategy,
                "terms": ctx.get("terms", []),
                "files": ctx.get("files", []),
            },
        )
    except Exception as exc:
        emit("retrieval", {"strategy": "unavailable", "error": str(exc), "files": []})

    skills_block = "(none matched)"
    try:
        receipt = skills_mod.scan_skills_for_task(
            str(repo), str(request), retrieval_terms=[]
        )
        skills_block = str(receipt.get("skills_block") or skills_block)
        emit("skills", receipt)
    except Exception as exc:
        emit("skills", {"skipped": "unavailable", "error": str(exc)})

    memory_block = "(none recorded yet)"
    try:
        decisions = memory_mod.query_planning_decisions(
            str(repo), str(request), retrieval_terms=[], limit=6
        )
        memory_block = memory_mod.render_memory_block(decisions) or memory_block
        emit("decision_memory", {"matched": len(decisions or [])})
    except Exception as exc:
        emit("decision_memory", {"matched": 0, "error": str(exc)})

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": AGENT_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## Task\n{request}\n\n"
                f"## Repo\n{repo}\n\n"
                f"## Retrieved context ({strategy})\n{context_block}\n\n"
                f"## Applicable skills\n{skills_block}\n\n"
                f"## Past decisions in this repo\n{memory_block}\n\n"
                "Begin. Reply with exactly ONE tool call."
            ),
        },
    ]
    # A resumed run continues from the replayed prefix. The prefix is ORDINARY
    # history: `step` has no resume concept, because where a conversation came
    # from is not a decision the loop should be making.
    if str(resume_history or "").strip():
        hist_text = str(resume_history).strip()[:4000]
        messages.append(
            {
                "role": "user",
                "content": (
                    "## Prior session context (resumed task - history replay, not "
                    "a restart; the tree already holds the earlier work)\n"
                    + hist_text
                    + "\nContinue with one tool call."
                ),
            }
        )
        emit(
            "steering",
            {"attempt": 0, "at": "agent-resume-history", "texts": [hist_text]},
        )

    steer = (
        steering_mod.SteeringBuffer(
            log_dir, tid, max_pending=int(cfg.get("max_pending_steering", 16))
        )
        if cfg.get("steering_enabled")
        else None
    )
    queue = steering_mod.QueuedSteering(steer, task_id=tid)
    session = BashSession(
        str(repo),
        int(cfg.get("command_timeout_s", 120)),
        int(cfg.get("max_output_chars", 3000)),
        cancellation_token=_new_cancel_token(),
    )
    toolbox = HarnessToolbox(
        str(repo),
        cfg,
        model,
        session,
        log_dir,
        files_touched,
        trace,
        emit,
        started=started,
        approve_fn=approve_fn,
        steering_queue=queue if steer is not None else None,
        fetch_budget=[int(cfg.get("agent_max_fetches", 4))],
        messages=messages,
    )
    step_config = step_mod.StepConfig.from_config(cfg)

    history = [
        step_mod.HistoryEntry(role=m["role"], content=m["content"]) for m in messages
    ]
    events = step_mod.step(history, toolbox, step_config)

    terminal: Dict[str, Any] = {}
    for event in events:
        kind = str(event.get("kind") or "")
        data = dict(event.get("data") or {})
        if kind == step_mod.TERMINAL_KIND:
            terminal = data
            emit(kind, data)
            continue
        emit(kind, data)

    status = step_mod.completion_status(terminal.get("status"))
    answer = str(terminal.get("answer") or "")
    emit(
        "result",
        {
            "status": status,
            "attempts": 1,
            "cost_usd": round(toolbox.spent_usd(), 6),
            "task_id": tid,
        },
    )
    if queue is not None and queue.receipt().get("deliveries"):
        emit("steering_queue", queue.receipt())

    diff = ""
    try:
        from harness import editor as editor_mod

        diff = editor_mod.unified_diff(pristine, repo)
    except Exception:  # pragma: no cover — a diff failure is not a run failure
        diff = ""

    output = AgentResult(
        {
            "mode": "agent",
            "status": status,
            "kernel_status": status,
            "agent_strategy": STEPPED_STRATEGY,
            "agent_strategy_source": "config",
            "agent_strategy_resolver": "harness.agent_loop.run_agent_stepped",
            "answer": answer,
            "task_id": tid,
            "run_id": tid,
            "trace_path": str(log_dir / "trace.jsonl"),
            "checkpoint_path": "",
            "diff": diff,
            "files_touched": list(terminal.get("files_touched") or files_touched),
            "cost_usd": round(toolbox.spent_usd(), 6),
            "model_calls": [],
            "verification": terminal.get("verification") or {},
            "reflection": terminal.get("reflection") or {},
        }
    )
    if terminal.get("reason"):
        output["end_reason"] = str(terminal["reason"])
    if queue is not None and (
        queue.receipt().get("deliveries") or queue.receipt().get("queued")
    ):
        output["steering_queue"] = queue.receipt()
    return output


def _json_safe_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in config.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        result[str(key)] = value
    return result


def _jsonable(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return repr(obj)


def _context_block(
    repo_path: str, rel_files: List[str], max_lines: int, max_files: int
) -> str:
    try:
        max_files = max(1, min(20, int(max_files)))
    except (TypeError, ValueError):
        max_files = 4
    try:
        max_lines = max(1, min(500, int(max_lines)))
    except (TypeError, ValueError):
        max_lines = 80
    parts: List[str] = []
    for rel in rel_files[:max_files]:
        p = _safe_join(Path(repo_path), str(rel))
        try:
            if p is None or not p.is_file() or p.stat().st_size > 200_000:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        shown = (
            [*lines[:max_lines], f"... [{len(lines) - max_lines} more lines]"]
            if len(lines) > max_lines
            else lines
        )
        display = str(rel).replace("\\", "/")
        parts.append(f"### {display}\n```\n" + "\n".join(shown) + "\n```")
    return "\n\n".join(parts) or "(no retrieved files)"


def _orig_file(log_dir: Path, rel: str) -> Path:
    root = log_dir / "orig"
    safe = _safe_join(root, rel)
    return safe if safe is not None else root / "__invalid_path__"


def _stash_original(log_dir: Path, full: Path, rel: str) -> None:
    """Save the pre-edit original once (idempotent; powers /diff undo)."""
    dest = _orig_file(log_dir, rel)
    if dest.exists():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if full.is_file():
            dest.write_bytes(full.read_bytes())
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            (dest.parent / (dest.name + ".absent")).touch()
    except OSError:
        pass


def _stash_pristine_original(log_dir: Path, pristine: Path, rel: str) -> None:
    """Stash a first-seen original from the pristine tree for BASH undo."""
    dest = _orig_file(log_dir, rel)
    if dest.exists():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        source = _safe_join(pristine, rel)
        if source is not None and source.is_file() and not source.is_symlink():
            dest.write_bytes(source.read_bytes())
        else:
            (dest.parent / (dest.name + ".absent")).touch()
    except OSError:
        pass


def _execute_tool(
    repo: str,
    tool: str,
    args: Dict[str, Any],
    cfg: Dict[str, Any],
    session: Any,
    log_dir: Path,
    files_touched: List[str],
    trace: TraceLogger,
    emit: Callable,
    messages: List[Dict[str, str]],
    budgets: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Execute one parsed tool call against the live repo.

    Returns (ok, model-facing output). EDIT/WRITE stash originals for
    undo before mutating. BASH runs LIVE (local subprocess, never
    Docker) with the deny-guard + cwd tracking; Ctrl+C during BASH
    stops that call (a tool error), never the session. FETCH is
    read-only web (GET-only, SSRF-guarded, capped — never a shell).
    MCP/plugin verbs route through route_tool(); MCP failures degrade
    to TOOL ERROR kinds (never a traceback). Unknown tools answer
    honestly. Never raises (errors become messages), except
    KeyboardInterrupt from the MODEL path (handled by the caller).
    """
    from harness import editor

    protected = [str(p) for p in (cfg.get("protected_paths") or [])]
    repo_p = Path(repo)
    if tool == "read":
        rel = str(args.get("path") or "")
        full = _safe_join(repo_p, rel)
        if full is None:
            return False, f"READ refused: {rel!r} is not a safe repo-relative path."
        cap = int(cfg.get("agent_max_read_chars", 12000))
        try:
            if not full.is_file():
                return False, f"READ miss: {rel} does not exist."
            if full.stat().st_size > 200_000:
                return False, f"READ miss: {rel} is too large to inline."
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"READ miss: {rel} could not be read ({exc})."
        if len(text) > cap:
            text = text[:cap] + "\n…[truncated]"
        trace.log("tool_result_detail", {"tool": "read", "path": rel})
        return True, f"READ {rel}:\n```\n{text}\n```"

    if tool == "glob":
        pattern = str(args.get("pattern") or "**/*.py").strip() or "**/*.py"
        pattern_path = Path(pattern.replace("\\", "/"))
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            return False, "GLOB refused: pattern must stay inside the repository."
        hits: List[str] = []
        try:
            for hit in _globmod.glob(str(repo_p / pattern), recursive=True):
                try:
                    rel = Path(hit).relative_to(repo_p).as_posix()
                except ValueError:
                    continue
                if any(part in _SKIP_DIRS for part in rel.split("/")):
                    continue
                if _safe_join(repo_p, rel) is None:
                    continue
                hits.append(rel)
                if len(hits) >= 100:
                    break
        except Exception as exc:
            return False, f"GLOB failed: {exc}"
        hits.sort()
        if not hits:
            return True, f"GLOB {pattern}: no matches."
        shown = "\n".join(hits[:60]) + (
            f"\n…[{len(hits) - 60} more]" if len(hits) > 60 else ""
        )
        return True, f"GLOB {pattern} ({len(hits)}):\n{shown}"

    if tool == "grep":
        pattern = str(args.get("pattern") or "")
        sub = str(args.get("path") or "").strip()
        if not pattern:
            return False, "GREP needs a pattern."
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return False, f"GREP bad pattern: {exc}"
        if sub:
            safe_sub = _safe_join(repo_p, sub)
            if safe_sub is None:
                return False, "GREP refused: path must stay inside the repository."
            roots = [safe_sub]
        else:
            roots = [repo_p]
        hits: List[str] = []
        for root in roots:
            base = root if root.is_dir() else root.parent
            for dirpath, dirnames, filenames in __import__("os").walk(base):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for name in filenames:
                    fp = Path(dirpath) / name
                    try:
                        if fp.stat().st_size > 200_000:
                            continue
                        text = fp.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    for i, line in enumerate(text.splitlines(), start=1):
                        if rx.search(line):
                            try:
                                rel = fp.relative_to(repo_p).as_posix()
                            except ValueError:
                                continue
                            hits.append(f"{rel}:{i}: {line.strip()[:160]}")
                            if len(hits) >= 50:
                                break
                    if len(hits) >= 50:
                        break
                if len(hits) >= 50:
                    break
        if not hits:
            return True, f"GREP {pattern!r}: no matches."
        return True, f"GREP {pattern!r} ({len(hits)}):\n" + "\n".join(hits)

    if tool == "bash":
        command = str(args.get("command") or "").strip()
        if not command:
            return False, "BASH needs a command."
        # No shell-via-fetch: a bare FETCH line is never a shell command.
        try:
            from harness import webfetch as webfetch_mod

            if webfetch_mod.parse_fetch(command) is not None:
                return False, (
                    "That looks like a FETCH — use the fetch tool "
                    '({"tool": "fetch", "url": ...}), not BASH.'
                )
        except Exception:
            pass
        try:
            try:
                out = _run_bash_live(session, command, cfg)
            except KeyboardInterrupt:
                return (
                    False,
                    "BASH interrupted by user — the call stopped, the session continues.",
                )
        except PermissionError as exc:
            return False, f"COMMAND REJECTED: {exc}"
        except Exception as exc:
            if type(exc).__name__ == "SandboxUnavailableError":
                return False, f"sandbox unavailable: {exc}"
            try:
                from harness.tool_errors import classify_exception as _ce

                err = _ce(exc, command)
                return False, f"TOOL ERROR [{err.kind}]: {err.detail}"
            except Exception:
                return False, f"TOOL ERROR: {exc}"
        return True, out

    if tool == "edit":
        rel = str(args.get("path") or "")
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        if not rel or old is None or new is None:
            return False, "EDIT needs path + old_string + new_string."
        old_s, new_s = str(old), str(new)
        if not old_s:
            return False, "EDIT old_string must not be empty (use WRITE to create)."
        full = _safe_join(repo_p, rel)
        if full is None:
            return False, f"EDIT refused: {rel!r} is not a safe repo-relative path."
        if editor.is_protected(rel, protected):
            return False, f"EDIT refused: {rel} is protected."
        try:
            if not full.is_file():
                return False, f"EDIT miss: {rel} does not exist (use WRITE to create)."
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"EDIT miss: {exc}"
        count = text.count(old_s)
        if count == 0:
            return False, "EDIT miss: old_string not found verbatim (check whitespace)."
        _stash_original(log_dir, full, rel)
        try:
            full.write_text(text.replace(old_s, new_s, 1), encoding="utf-8")
        except OSError as exc:
            return False, f"EDIT failed: {exc}"
        norm = rel.replace("\\", "/")
        syntax_ok, syntax_message = editor.syntax_check(repo_p.as_posix(), [norm])
        if not syntax_ok:
            return False, f"EDIT syntax check failed: {syntax_message}"
        if norm not in files_touched:
            files_touched.append(norm)
        note = "" if count == 1 else f" ({count} matches; replaced the first)"
        emit("edit_applied", {"path": norm, "replacements": 1, "note": note.strip()})
        return True, f"EDIT {rel}: replaced 1 block{note}."

    if tool == "write":
        rel = str(args.get("path") or "")
        content = str(args.get("content") or "")
        if not rel:
            return False, "WRITE needs a path."
        full = _safe_join(repo_p, rel)
        if full is None:
            return False, f"WRITE refused: {rel!r} is not a safe repo-relative path."
        if editor.is_protected(rel, protected):
            return False, f"WRITE refused: {rel} is protected."
        _stash_original(log_dir, full, rel)
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
        except OSError as exc:
            return False, f"WRITE failed: {exc}"
        norm = rel.replace("\\", "/")
        syntax_ok, syntax_message = editor.syntax_check(repo_p.as_posix(), [norm])
        if not syntax_ok:
            return False, f"WRITE syntax check failed: {syntax_message}"
        if norm not in files_touched:
            files_touched.append(norm)
        emit("edit_applied", {"path": norm, "bytes": len(content)})
        return True, f"WRITE {rel}: wrote {len(content)} chars."

    if tool == "memory":
        from harness import decision_memory as dm

        try:
            mem = dm.query_planning_decisions(
                repo_path=repo,
                issue_text=str(args.get("query") or ""),
                limit=int(cfg.get("memory_query_limit", 6)),
            )
            block = dm.render_memory_block(
                mem["decisions"], max_chars=int(cfg.get("memory_max_chars", 1500))
            )
        except Exception as exc:
            return True, f"MEMORY: unavailable ({exc})."
        trace.log(
            "decision_memory",
            {"query": str(args.get("query") or ""), "matched": len(mem["decisions"])},
        )
        return True, f"MEMORY ({len(mem['decisions'])}):\n{block}"

    if tool == "verify":
        if not (cfg.get("target_test") or cfg.get("test_command")):
            return (
                True,
                "VERIFY: no tests declared for this task (target_test/test_command unset) — rely on BASH runs.",
            )
        res = _run_verify(repo, cfg, trace, emit)
        tail = (res.get("raw") or "")[-1500:]
        verdict = (
            ("PASS" if res.get("target_passed") else "FAIL")
            + " target / "
            + ("PASS" if res.get("regression_passed") else "FAIL")
            + " suite"
        )
        return bool(res.get("target_passed")), f"VERIFY {verdict}\n{tail}"

    if tool == "fetch":
        url = str(args.get("url") or "").strip().strip("`\"'")
        if not url:
            return False, "FETCH needs a url."
        if not cfg.get("agent_fetch_enabled", True):
            return True, (
                "FETCH is disabled for this task (agent_fetch_enabled=False) "
                "— answer from repo context + general knowledge."
            )
        left = None
        try:
            if budgets is not None and "fetch_left" in budgets:
                left = budgets["fetch_left"]
                if int(left[0]) <= 0:
                    return True, (
                        "FETCH budget exhausted — proceed with bash "
                        "commands, or DONE with your summary."
                    )
        except Exception:
            left = None
        try:
            from harness import webfetch as webfetch_mod

            if webfetch_mod.parse_fetch(f"FETCH {url}") is None:
                return False, f"FETCH refused: {url!r} is not an http(s) URL."
            msg, res = webfetch_mod.fetch_and_render(
                url,
                timeout_s=int(cfg.get("webfetch_timeout_s", 15)),
                max_bytes=int(cfg.get("webfetch_max_bytes", 1_048_576)),
                max_chars=int(cfg.get("webfetch_max_chars", 3000)),
                max_redirects=int(cfg.get("webfetch_max_redirects", 3)),
                audit_hook=lambda r: trace.log(
                    "web_fetch",
                    {
                        "url": r.url,
                        "ok": r.ok,
                        "status": r.status,
                        "chars": len(r.text),
                    },
                ),
            )
            if left is not None:
                try:
                    left[0] = int(left[0]) - 1
                except Exception:
                    pass
            emit(
                "tool_result_detail",
                {"tool": "fetch", "url": res.url, "ok": res.ok, "status": res.status},
            )
            return bool(res.ok), f"FETCH {res.url} ({res.status}):\n{msg[:4000]}"
        except Exception as exc:
            return False, f"TOOL ERROR [internal_error]: fetch failed ({exc})"

    if tool in ("mcp", "mcp_call"):
        server = str(args.get("server") or "").strip()
        name = str(args.get("name") or args.get("tool") or "").strip()
        tool_args = args.get("args", {})
        if not server or not name:
            return False, 'MCP needs "server" + "name" (+ optional "args" object).'
        if not isinstance(tool_args, dict):
            return False, "MCP args must be a JSON object."
        launch = _resolve_mcp_server(server, cfg)
        if launch is None:
            return False, (
                f"unknown MCP server {server!r} — installed servers: "
                f"{', '.join(_mcp_server_labels(cfg)) or '(none)'}"
            )
        try:
            from memory.mcp_client import call_mcp_tool as _mcp_call

            out = _mcp_call(launch, name, tool_args, cwd=repo)
        except Exception as exc:
            return False, f"TOOL ERROR [internal_error]: mcp call failed ({exc})"
        try:
            if isinstance(out, dict) and not out.get("ok"):
                return (
                    False,
                    f"TOOL ERROR [internal_error]: mcp {name} failed ({out.get('error')})",
                )
            text = out.get("text", "") if isinstance(out, dict) else str(out)
            emit("tool_result_detail", {"tool": "mcp", "server": server, "name": name})
            return True, f"MCP {server}/{name}:\n{str(text)[:4000]}"
        except Exception as exc:
            return False, f"TOOL ERROR [internal_error]: mcp result unreadable ({exc})"

    # Plugin verbs (installed tool extensions) via the one router.
    try:
        routed = route_tool(tool, args, cfg)
    except Exception:
        routed = {"kind": "unknown"}
    if routed.get("kind") == "plugin":
        rest = str(args.get("command") or args.get("args") or "").strip()
        full_cmd = f"{tool} {rest}".strip()
        if not _plugin_command_ok(full_cmd, cfg):
            return False, (
                f"PLUGIN REJECTED: {full_cmd!r} is not a simple read-only "
                "plugin-verb command — re-issue within the verb's read-only shape."
            )
        try:
            try:
                out = _run_bash_live(session, full_cmd, cfg)
            except KeyboardInterrupt:
                return (
                    False,
                    "interrupted by user — the call stopped, the session continues.",
                )
        except PermissionError as exc:
            return False, f"COMMAND REJECTED: {exc}"
        except Exception as exc:
            try:
                from harness.tool_errors import classify_exception as _ce2

                err = _ce2(exc, full_cmd)
                return False, f"TOOL ERROR [{err.kind}]: {err.detail}"
            except Exception:
                return False, f"TOOL ERROR: {exc}"
        emit("tool_result_detail", {"tool": tool, "command": full_cmd})
        return True, out

    return False, f"unknown tool {tool!r}."


def _plugin_command_ok(full_cmd: str, cfg: Dict[str, Any]) -> bool:
    """True when a plugin-verb command may run (read-only shape).

    Assumes full_cmd is "<verb> <tail>". The command must start with a
    known plugin verb (config + installed plugins) AND carry no shell
    composition metacharacters (the same forbidden set as BATCH).
    Never raises.
    """
    try:
        from harness.tools import _BATCH_FORBIDDEN

        verbs = [str(v) for v in _plugin_verbs(cfg) if str(v).strip()]
        low = (full_cmd or "").strip().lower()
        if not any(low == v.lower() or low.startswith(v.lower() + " ") for v in verbs):
            return False
        return _BATCH_FORBIDDEN.search(full_cmd) is None
    except Exception:
        return False


def _mcp_server_labels(cfg: Dict[str, Any]) -> List[str]:
    """Known MCP server labels (config + connectors + plugins). Never raises."""
    labels: List[str] = []
    try:
        cfg_map = cfg.get("agent_mcp_servers") or {}
        if isinstance(cfg_map, dict):
            labels.extend(str(k) for k in cfg_map)
    except Exception:
        pass
    resolver = cfg.get("mcp_server_labels_resolver")
    if callable(resolver):
        try:
            discovered = resolver()
            if isinstance(discovered, dict):
                labels.extend(str(key) for key in discovered)
            elif isinstance(discovered, (list, tuple, set)):
                labels.extend(str(value) for value in discovered)
        except Exception:
            pass
    return sorted(set(labels))


def _approval_preview(repo: str, tool: str, args: Dict[str, Any]) -> str:
    if tool == "fetch":
        return str(args.get("url") or "")[:2000]
    if tool in ("mcp", "mcp_call"):
        try:
            return (
                f"{args.get('server', '')} {args.get('name', '')}\n"
                + json.dumps(args.get("args", {}), ensure_ascii=False)[:1500]
            )
        except Exception:
            return f"{args.get('server', '')} {args.get('name', '')}"
    if tool == "bash":
        return str(args.get("command") or "")[:2000]
    if tool == "edit":
        return f"{args.get('path', '')}\n--- old ---\n{str(args.get('old_string') or '')[:1200]}"
    if tool == "write":
        return f"{args.get('path', '')}\n{str(args.get('content') or '')[:2000]}"
    return json.dumps(args, ensure_ascii=False)[:2000]


def _verify_boundary_names(fn: Callable[..., Any], name: str) -> bool:
    """Whether a resolved verifier EXPLICITLY declares ``name`` in its signature.

    VEX-PF-10. Presence of ``**kwargs`` is NOT evidence: a scripted double or
    ``harness/_stubs/verify.py`` would accept the rung keywords and then hand
    them to something that rejects them, which is a run-killing ``TypeError``
    rather than a degraded receipt. This is the same rule
    ``harness.model_client._boundary_names`` applies to ``effort`` and
    ``harness.tools.BashSession`` applies to ``cancellation_token``. The real
    boundary, ``execution.verify.verify``, names all three, so production is
    fully wired; an uninspectable callable gets the historical call.
    """
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters


def _run_verify(
    repo: str, cfg: Dict[str, Any], trace: TraceLogger, emit: Callable
) -> Dict[str, Any]:
    """Run the declared tests once; returns a plain-dict verdict.

    VEX-PF-10: this is the general agent's ONE declared-test run, and it is the
    only place the daily interactive session touches a verifier. It therefore
    carries the same rung configuration ``harness/core.py``'s final gate does,
    so the flake gate and the baseline set reach an interactive session exactly
    as they reach a benchmark run. Without this the verification intelligence
    was structurally unreachable from the path a person actually uses.

    The keywords are forwarded only when the resolved boundary NAMES them
    (``_verify_boundary_names``), for the same reason
    ``harness/model_client`` gates ``effort``: a scripted double or
    ``harness/_stubs/verify.py`` carries the historical signature, and handing
    it an undeclared keyword is a run-killing ``TypeError``.
    """
    try:
        from harness.deps import get_verify

        boundary = get_verify()
        kwargs: Dict[str, Any] = {
            "test_command": cfg.get("test_command"),
            "verify_timeout_s": int(cfg.get("verify_timeout_s", 300)),
        }
        if _verify_boundary_names(boundary, "rung_config"):
            kwargs["rung_config"] = cfg
            kwargs["run_dir"] = str(getattr(trace, "log_dir", "") or "")
            kwargs["phase"] = "postfix"
        v = boundary(
            repo,
            cfg.get("target_test"),
            rerun_for_flake_check=int(cfg.get("baseline_reruns", 1)),
            **kwargs,
        )
        out = {
            "target_passed": v.target_test_passed,
            "regression_passed": v.regression_passed,
            "flaky": v.flaky,
            "raw": v.raw_output,
            # VEX-PF-10: which mechanism produced this verdict, plus the
            # three-valued flake reading. `flaky: false` is ambiguous on its
            # own — it means both "we checked and it was stable" and "we never
            # checked" — and this is the row `cli/tracelog.py` reads, so the
            # disambiguation has to travel with it.
            "rung": getattr(v, "verification_rung", ""),
            "rungs": list(getattr(v, "verification_rungs", ()) or ()),
            "flake_check": getattr(v, "flake_check", ""),
            "repetitions": getattr(v, "repetitions", None),
            "observed_outcomes": list(getattr(v, "observed_outcomes", ()) or ()),
        }
        emit(
            "verify",
            {
                "target_passed": out["target_passed"],
                "regression_passed": out["regression_passed"],
                "flaky": out["flaky"],
                "rung": out["rung"],
                "rungs": out["rungs"],
                "flake_check": out["flake_check"],
                "repetitions": out["repetitions"],
                "observed_outcomes": out["observed_outcomes"],
                "raw": (v.raw_output or "")[-2000:],
            },
        )
        return out
    except Exception as exc:
        emit("verify", {"target_passed": False, "error": str(exc)})
        return {
            "target_passed": False,
            "regression_passed": False,
            "flaky": False,
            "raw": str(exc),
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Diff + undo (live repo vs the pristine reference)
# ---------------------------------------------------------------------------


def agent_diff(task_id: str, log_root: Path, repo_path: str) -> str:
    """Unified diff of the live repo vs this task's pristine reference.

    Assumes task_id/log_root identify an agent session (pristine/ exists)
    and repo_path is the live repo it edited. Returns "" when nothing
    changed or the reference is missing. Never raises.
    """
    try:
        from harness import editor

        pristine = Path(log_root) / _safe_agent_id(task_id) / "pristine"
        if not pristine.is_dir():
            return ""
        return editor.unified_diff(str(pristine), str(repo_path)) or ""
    except Exception:
        return ""


def undo_edits(
    task_id: str,
    log_root: Path,
    repo_path: str,
    steps: int = 1,
    targets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Undo agent edits to the live repo (steps>=1 files, one file, or all).

    Restores from logs/{task_id}/orig/ originals stashed before the first
    edit of each path (steps="all" restores every stashed path, newest
    first; a positive int restores that many; targets=[rel...] restores
    exactly those files regardless of steps). Files created by the agent
    (original absent) are deleted. Appends an "undo" event to the task's
    trace.jsonl (best-effort). Never touches pristine/ (diff reference
    stays intact) and never raises. Returns {restored, deleted, missing}.
    """
    out: Dict[str, Any] = {"restored": [], "deleted": [], "missing": []}
    try:
        safe_task_id = _safe_agent_id(task_id)
        orig_root = Path(log_root) / safe_task_id / "orig"
        if not orig_root.is_dir():
            return out
        wanted: Optional[set] = None
        if targets:
            wanted = {
                str(t).replace("\\", "/").strip().strip("/")
                for t in targets
                if str(t).strip()
            }
        stashed: List[Tuple[float, Path]] = []
        for p in orig_root.rglob("*"):
            if p.is_file() and not p.name.endswith(".absent"):
                try:
                    rel = p.relative_to(orig_root).as_posix()
                except ValueError:
                    continue
                if wanted is not None and rel not in wanted:
                    continue
                try:
                    stashed.append((p.stat().st_mtime, p))
                except OSError:
                    continue
        stashed.sort(reverse=True)
        absent = list(orig_root.rglob("*.absent"))
        if wanted is not None or (isinstance(steps, str) and steps == "all"):
            picks = stashed
        else:
            try:
                n = max(1, int(steps))
            except (TypeError, ValueError):
                n = 1
            picks = stashed[:n]
        repo_p = Path(repo_path)
        for _, src in picks:
            rel = src.relative_to(orig_root).as_posix()
            dest = _safe_join(repo_p, rel)
            if dest is None:
                out["missing"].append(rel)
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src.read_bytes())
                out["restored"].append(rel)
            except OSError:
                out["missing"].append(rel)
        if wanted is not None or (isinstance(steps, str) and steps == "all"):
            for marker in absent:
                rel = marker.relative_to(orig_root).as_posix()[: -len(".absent")]
                if wanted is not None and rel not in wanted:
                    continue
                dest = _safe_join(repo_p, rel)
                if dest is None:
                    out["missing"].append(rel)
                    continue
                try:
                    if dest.is_file() or dest.is_symlink():
                        dest.unlink()
                        out["deleted"].append(rel)
                except OSError:
                    out["missing"].append(rel)
        if wanted is not None and wanted:
            known = set(out["restored"]) | set(out["deleted"]) | set(out["missing"])
            for w in sorted(wanted - known):
                out["missing"].append(w)
        try:
            trace_file = Path(log_root) / safe_task_id / "trace.jsonl"
            if trace_file.parent.is_dir():
                import json as _json
                import time as _time

                with trace_file.open("a", encoding="utf-8") as fh:
                    fh.write(
                        _json.dumps(
                            {
                                "kind": "undo",
                                "ts": _time.time(),
                                "data": {
                                    "restored": out["restored"],
                                    "deleted": out["deleted"],
                                    "missing": out["missing"],
                                },
                            }
                        )
                        + "\n"
                    )
        except Exception:
            pass
    except Exception:
        pass
    return out
