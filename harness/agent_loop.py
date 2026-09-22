"""General coding-agent loop — the interactive `vex` engine (not `vex fix`).

`harness.core.run_task` is the verifier-gated benchmark path: pristine/work
snapshots, planner, verifier-gated success. That path is UNCHANGED and stays
the implementation behind `vex fix`.

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
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness.config import get_config
from harness.model_client import ModelClient
from harness.trace import TraceLogger

__all__ = [
    "AgentIntent",
    "AgentResult",
    "agent_diff",
    "classify_agent_input",
    "classify_deterministic",
    "load_resume_history",
    "render_agent_plan",
    "route_tool",
    "run_agent",
    "undo_edits",
]

AGENT_SYSTEM = """\
You are Vex, a general coding agent working LIVE in the user's repository.
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
    r"how does (this|it|vex) work",
    r"what does (this|it|vex) do",
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
    r"show\s+me|what\s|how\s|why\s|where\s|which\s|who\s|when\s)",
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
_BUILD_RE = re.compile(
    r"\b(add|create|build|implement|generate|introduce|extend|support|write)\b",
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
            "meta question about vex",
            "I'm vex — I work on this repo with you: explain code, make changes, "
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
    return AgentIntent("chit_chat", "unknown")


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
# ---------------------------------------------------------------------------

_TOOL_NAMES = (
    "read",
    "glob",
    "grep",
    "bash",
    "edit",
    "write",
    "memory",
    "fetch",
    "mcp",
    "mcp_call",
    "verify",
    "done",
)

_PLAIN_PATTERNS = (
    ("read", re.compile(r"^\s*READ\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("glob", re.compile(r"^\s*GLOB\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("grep", re.compile(r"^\s*GREP\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("bash", re.compile(r"^\s*BASH\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("memory", re.compile(r"^\s*MEMORY\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("fetch", re.compile(r"^\s*FETCH\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)),
    ("verify", re.compile(r"^\s*VERIFY\s*$", re.IGNORECASE)),
    ("done", re.compile(r"^\s*DONE(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)),
)


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", t, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else t


def parse_tool_call(
    text: str, known_verbs: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """Parse one model reply into {"tool": name, ...args}.

    Accepts the JSON form (bare or fenced) and the plain-text one-line
    form. known_verbs (plugin first-tokens, from the session config)
    additionally lets {"tool": "<verb>", ...} through — routing and
    read-only validation happen at execution. Returns None when the
    reply is not a recognizable tool call. Never raises.
    """
    try:
        t = _strip_fences(text or "")
        if not t:
            return None
        if t.lstrip().startswith("{"):
            try:
                obj = json.loads(re.search(r"\{.*\}", t, re.DOTALL).group(0))  # type: ignore[union-attr]
            except (ValueError, AttributeError):
                obj = None
            if isinstance(obj, dict):
                tool = str(obj.get("tool") or "").strip().lower()
                extra = {str(v).lower() for v in (known_verbs or []) if str(v).strip()}
                if tool in _TOOL_NAMES or tool in extra:
                    out = {"tool": tool}
                    for k, v in obj.items():
                        if k != "tool":
                            out[k] = v
                    return out
        for name, pat in _PLAIN_PATTERNS:
            m = pat.match(t)
            if not m:
                continue
            if name == "read":
                return {"tool": "read", "path": m.group(1).strip().strip("`\"'")}
            if name == "glob":
                return {"tool": "glob", "pattern": m.group(1).strip().strip("`\"'")}
            if name == "grep":
                rest = m.group(1).strip()
                parts = rest.split(None, 1)
                # GREP "some pattern" path  -> pattern may be quoted
                if len(parts) == 2 and not parts[1].startswith("-"):
                    return {
                        "tool": "grep",
                        "pattern": parts[0].strip("\"'"),
                        "path": parts[1].strip(),
                    }
                return {"tool": "grep", "pattern": rest.strip("\"'")}
            if name == "bash":
                return {"tool": "bash", "command": m.group(1).strip()}
            if name == "memory":
                return {"tool": "memory", "query": m.group(1).strip()}
            if name == "fetch":
                return {"tool": "fetch", "url": m.group(1).strip().strip("`\"'")}
            if name == "verify":
                return {"tool": "verify"}
            if name == "done":
                return {"tool": "done", "answer": (m.group(1) or "").strip()}
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class AgentResult(dict):
    """run_agent outcome (a dict with an .ok helper)."""

    @property
    def ok(self) -> bool:
        return bool(self.get("status") == "success")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tox",
    "node_modules",
    ".idea",
    ".vscode",
    "logs",
}


def _safe_join(repo: Path, rel: str) -> Optional[Path]:
    clean = (rel or "").strip().strip("`\"'")
    if not clean:
        return None
    p = Path(clean)
    if p.is_absolute() or ".." in p.parts:
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
    try:
        from cli import plugins as plugins_mod  # type: ignore

        verbs.extend(plugins_mod.config_tool_verbs() or [])
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
    or a connector label (`vex mcp add`). Sources: config
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
    try:
        from cli import connectors as connectors_mod  # type: ignore

        cmd = connectors_mod.server_commands().get(str(label))
        if cmd:
            return str(cmd)
    except Exception:
        pass
    try:
        from cli import plugins as plugins_mod  # type: ignore

        for entry in plugins_mod.list_plugins():
            if entry.get("error"):
                continue
            if entry.get("enabled") is False:
                continue
            servers = entry.get("mcp_servers") or {}
            if isinstance(servers, dict) and str(label) in servers:
                return str(servers[str(label)])
    except Exception:
        pass
    return None


def _run_bash_live(session: Any, command: str, cfg: Dict[str, Any]) -> str:
    """Run one bash command LIVE on the host (not Docker).

    Assumes session is a harness.tools.BashSession (deny-guard, cwd
    tracking, output caps stay). Uses an injected fake sandbox when
    tests set one (deps override wins); otherwise runs the local
    subprocess stub directly so Docker never sees a live-repo command.
    Raises PermissionError from the deny guard; KeyboardInterrupt
    propagates to the caller (which turns it into a tool result).
    """
    from harness import deps as deps_mod

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
        tf = Path(log_root) / str(task_id or "") / "trace.jsonl"
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
    from harness import decision_memory, editor, retrieval
    from harness import steering as steering_mod

    cfg = get_config(config or {})
    tid = task_id or f"agent-{uuid.uuid4().hex[:8]}"
    root = Path(log_root) if log_root else Path(cfg.get("work_subdir", "logs"))
    log_dir = root / tid
    log_dir.mkdir(parents=True, exist_ok=True)
    trace = TraceLogger(log_dir)
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
    max_turns = int(cfg.get("agent_max_turns", 25))
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
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": AGENT_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## Task\n{request}\n\n"
                f"## Repo\n{repo}\n\n"
                f"## Retrieved context ({ctx.get('strategy', 'grep')})\n{context_block}\n\n"
                f"## Past decisions in this repo\n{memory_block}\n\n"
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

    from harness.tools import BashSession

    session = BashSession(
        str(repo),
        int(cfg.get("command_timeout_s", 120)),
        int(cfg.get("max_output_chars", 3000)),
    )
    fetch_budget = int(cfg.get("agent_max_fetches", 4))
    files_touched: List[str] = []
    verification: Optional[Dict[str, Any]] = None
    answer = ""
    status = "failed"
    turns_done = 0
    end_reason = ""

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

    for turn in range(1, max_turns + 1):
        turns_done = turn
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
        if steer is not None and steer.pending():
            if steer.has_intent("abort"):
                taken = steer.take("abort-agent-turn")
                _emit(
                    "steering_abort",
                    {
                        "attempt": turn,
                        "at": "agent-turn",
                        "texts": [e.text for e in taken],
                    },
                )
                answer = "aborted by user steering"
                status = "aborted"
                break
            if steer.has_intent("replan"):
                taken = steer.take("replan-agent-turn")
                _emit(
                    "steering_replan",
                    {
                        "attempt": turn,
                        "at": "agent-turn",
                        "texts": [e.text for e in taken],
                    },
                )
                guide = "User steering (re-plan, keep work so far): " + " | ".join(
                    e.text for e in taken
                )
                messages.append(
                    {
                        "role": "user",
                        "content": guide + "\nContinue with one tool call.",
                    }
                )
                continue
            taken = steer.take(f"agent-turn-{turn}")
            _emit(
                "steering",
                {
                    "attempt": turn,
                    "at": f"agent-turn-{turn}",
                    "seqs": [e.seq for e in taken],
                    "texts": [e.text for e in taken],
                },
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "USER STEERING (incorporate and continue):\n"
                        + "\n".join(f"- {e.text}" for e in taken)
                        + "\nContinue with exactly ONE tool call."
                    ),
                }
            )

        try:
            reply = model.call(messages, step=f"agent-{turn}")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _emit("task_end", {"status": "error", "reason": f"model failed: {exc}"})
            status = "error"
            break

        call = parse_tool_call(reply, [v.split()[0] for v in _plugin_verbs(cfg)])
        if call is None:
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "That was not a valid tool call. Reply with exactly ONE tool call "
                        '(JSON {"tool": ...} or READ/GLOB/GREP/BASH/MEMORY/VERIFY/DONE).'
                    ),
                }
            )
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
                messages.append(
                    {"role": "user", "content": msg + " Continue with one tool call."}
                )
                continue

        try:
            holder = {"fetch_left": [fetch_budget]}
            ok, output = _execute_tool(
                str(repo),
                tool,
                args,
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
        _emit("tool_result", {"output": output[:4000], "ok": ok})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"## Tool result ({tool}, {'ok' if ok else 'error'})\n{output[:4000]}\n\n"
                    "Continue with exactly ONE tool call, or DONE with your summary."
                ),
            }
        )
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
    return out


def _jsonable(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return repr(obj)


def _context_block(
    repo_path: str, rel_files: List[str], max_lines: int, max_files: int
) -> str:
    parts: List[str] = []
    for rel in rel_files[:max_files]:
        p = Path(repo_path, rel)
        try:
            if not p.is_file() or p.stat().st_size > 200_000:
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
        parts.append(f"### {rel}\n```\n" + "\n".join(shown) + "\n```")
    return "\n\n".join(parts) or "(no retrieved files)"


def _orig_file(log_dir: Path, rel: str) -> Path:
    return log_dir / "orig" / Path(rel.replace("\\", "/"))


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
        hits: List[str] = []
        try:
            for hit in _globmod.glob(str(repo_p / pattern), recursive=True):
                try:
                    rel = Path(hit).relative_to(repo_p).as_posix()
                except ValueError:
                    continue
                if any(part in _SKIP_DIRS for part in rel.split("/")):
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
        roots = [repo_p if not sub else (_safe_join(repo_p, sub) or repo_p)]
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
        _stash_original(log_dir, full, rel)
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
        except OSError as exc:
            return False, f"WRITE failed: {exc}"
        norm = rel.replace("\\", "/")
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
    try:
        from cli import connectors as connectors_mod  # type: ignore

        labels.extend(str(k) for k in connectors_mod.server_commands())
    except Exception:
        pass
    try:
        from cli import plugins as plugins_mod  # type: ignore

        for entry in plugins_mod.list_plugins():
            if entry.get("error"):
                continue
            if entry.get("enabled") is False:
                continue
            servers = entry.get("mcp_servers") or {}
            if isinstance(servers, dict):
                labels.extend(str(k) for k in servers)
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


def _run_verify(
    repo: str, cfg: Dict[str, Any], trace: TraceLogger, emit: Callable
) -> Dict[str, Any]:
    """Run the declared tests once; returns a plain-dict verdict."""
    try:
        try:
            from execution.verify import verify
        except ImportError:
            from harness._stubs.verify import verify  # type: ignore[no-redef]
        v = verify(
            repo,
            cfg.get("target_test"),
            rerun_for_flake_check=int(cfg.get("baseline_reruns", 1)),
            test_command=cfg.get("test_command"),
            verify_timeout_s=int(cfg.get("verify_timeout_s", 300)),
        )
        out = {
            "target_passed": v.target_test_passed,
            "regression_passed": v.regression_passed,
            "flaky": v.flaky,
            "raw": v.raw_output,
        }
        emit(
            "verify",
            {
                "target_passed": out["target_passed"],
                "regression_passed": out["regression_passed"],
                "flaky": out["flaky"],
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

        pristine = Path(log_root) / task_id / "pristine"
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
        orig_root = Path(log_root) / task_id / "orig"
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
            dest = repo_p / rel
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
                dest = repo_p / rel
                try:
                    if dest.is_file():
                        dest.unlink()
                        out["deleted"].append(rel)
                except OSError:
                    out["missing"].append(rel)
        if wanted is not None and wanted:
            known = set(out["restored"]) | set(out["deleted"]) | set(out["missing"])
            for w in sorted(wanted - known):
                out["missing"].append(w)
        try:
            trace_file = Path(log_root) / task_id / "trace.jsonl"
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
