"""Intent classification for multi-mode routing (Modes round, Task A).

Before any work happens on a user's input, it is classified into one of:

- ``fix``       a bug report — something is broken and must be repaired
- ``build``     a new feature / capability request (no pre-existing failing test)
- ``question``  asking about this codebase (or Vex itself) — read-only answer
- ``research``  investigate an unfamiliar library/approach/topic — read-only,
  web-assisted synthesis
- ``convo``     greetings / chit-chat / questions about Vex — answer inline,
  never launch anything
- ``ambiguous`` genuinely unclear — ONE clarifying question instead of a
  guess (a wrong task-run burns minutes and model budget; a question costs
  one line — the same cost asymmetry that drove the earlier ``hi``-bug fix)

Two tiers, deliberately:

1. **Deterministic rules first** (offline, free, instant): the clear cases —
   greetings, meta questions about Vex, bug language, source artifacts,
   explicit build/research markers, plain question shapes — never touch the
   model. This keeps the pinned interactive-session wiring tests offline and
   makes ``hi`` free to answer.

2. **ONE cheap model call for the gray zone**: input that carries none of
   the clear signals ("add better input validation" — build or fix?) is
   classified by a single model call with ``difficulty_hint="easy"`` (the
   router's adaptive routing then picks the cheap tier — classification
   does not need the expensive models). Pin ``intent_model`` in config to
   force a specific cheap model. Any model failure, unparseable reply, or
   unknown kind degrades to ``ambiguous`` — routing must never crash or
   silently guess over a broken classifier.

``intent_enabled`` (config, default True) is the OFF arm: when False the
classifier returns ``fix`` for everything, restoring the legacy
every-input-is-a-bug-report behavior (the pre-modes contract).

Signal tables are module-level data so the regression tests pin them
directly (same discipline as cli/intent.py — which this module supersedes
for the session loop; the cli module stays as the frozen record of the
original ``hi``-bug fix and its pinned tests).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["WORK_MODES", "Intent", "classify_deterministic", "classify_input"]

#: The four kinds that route to a handler (convo/ambiguous are answered by
#: the session itself and never start a task).
WORK_MODES = ("fix", "build", "question", "research")

#: All six classification outcomes (validation set for model replies).
_ALL_KINDS = (*WORK_MODES, "convo", "ambiguous")

_GREETINGS = {
    "hi",
    "hello",
    "hey",
    "yo",
    "hiya",
    "howdy",
    "sup",
    "good morning",
    "good afternoon",
    "good evening",
    "hi there",
    "hello there",
    "greetings",
    "salutations",
}

_THANKS = {
    "thanks",
    "thank you",
    "ty",
    "thx",
    "cheers",
    "cool",
    "nice",
    "great",
    "awesome",
    "ok",
    "okay",
    "k",
    "okey",
    "lol",
    "haha",
}

#: Questions ABOUT VEX (the tool) — answered inline, never a task.
_META_QUESTION_WORDS = (
    "vex",
    "this tool",
    "this cli",
    "this thing",
    "this harness",
    "this program",
    "this app",
    "this agent",
    "this assistant",
)

_META_QUESTION_PATTERNS = (
    r"what can you do",
    r"who are you",
    r"what are you",
    r"how do (i|you) (use|quit|exit|stop|cancel)",
    r"how does (this|it|vex) work",
    r"what does (this|it|vex) do",
    r"show me (your|the) (commands|help)",
    r"what commands",
    r"which commands",
    r"list (your )?commands",
    r"are you (an? )?(ai|agent|llm|bot|robot)",
    r"what model",
    r"which model",
)

_CONVO_PATTERNS = (
    r"how('s| is| are)? (you|it|things|it going|going)",
    r"what'?s up",
    r"you there",
    r"anyone (there|home)",
    r"tell me about yourself",
)

#: Failure/symptom vocabulary — the strongest fix-request signal (the
#: proven table from the earlier interactive intent gate).
#: NOTE: "raises/throws" appear ONLY inside the symptom compound
#: ("raises when", "raises if", "throws when") — a bare "must raise
#: ValueError" is SPEC language inside a feature request ("an empty list
#: must raise ValueError"), not a bug report; the bare forms were a real
#: defect caught by the Task-F live session (a build request about a
#: contract was misrouted to fix).
_BUG_LANGUAGE = re.compile(
    r"\b("
    r"bug|error|fail(s|ed|ing|ure)?|crash(es|ed|ing)?|exception|traceback|"
    r"broken|breaks|broke|regression|regressed|flak(e|y)|flaky|"
    r"wrong(ly)?|incorrect(ly)?|inaccurate|off by|inverted|reversed|"
    r"swapped|truncated|duplicated|missing|omitted|ignored|skipped|"
    r"doesn'?t work|does not work|not working|won'?t|will not|can'?t|cannot|"
    r"returns? (the )?wrong|always returns|never returns|unexpected|"
    r"raises? (when|if|on)|throws? (when|if|on)|panics?|deadlocks?|leaks?|"
    r"overflows?|underflows?|"
    r"deprecated|typo|miscalculat|off-by-one|edge ?case|fails? when|"
    r"breaks? when|hangs?|times? out|timeout"
    r")\b",
    re.IGNORECASE,
)

#: Source artifacts — a fix request usually names WHERE it hurts.
_ARTIFACT_PATTERN = re.compile(
    r"(\.(py|js|ts|go|rs|java|c|h|cpp|rb|php|sh|yaml|yml|json|toml|md)\b"
    r"|[A-Za-z_][A-Za-z0-9_]{2,}\(\)"  # foo()
    r"|[A-Za-z_][A-Za-z0-9_]{2,}\.[a-z]{2,3}\b"  # module.py
    r"|test_[a-z0-9_]+|src/|tests?/|lib/|src\\|tests?\\|lib\\"
    r"|[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_]"
    r")",
    re.IGNORECASE,
)

#: Verbs that begin a BUILD request (create something new). Must co-occur
#: with an object marker (below) to fire — "make it the mean" alone is a
#: fix instruction, not a feature request.
_BUILD_VERBS = re.compile(
    r"^\s*(please\s+)?(can you\s+|could you\s+|i want to\s+|i need to\s+)?"
    r"(add|implement|create|build|write|make|generate|introduce|extend|support)\b",
    re.IGNORECASE,
)

#: Nouns marking the OBJECT of a build request.
_BUILD_OBJECTS = re.compile(
    r"\b(function|method|feature|capability|command|endpoint|module|class|"
    r"flag|option|utils?|helper|middleware|decorator|handler|parser|"
    r"validator|integration|subcommand)\b|\bnew file\b",
    re.IGNORECASE,
)

#: An article right after the build verb ("build a dashboard") also marks a
#: build object even without a known noun.
_ARTICLE_OBJECT = re.compile(
    r"^\s*(please\s+)?(can you\s+|could you\s+|i want to\s+|i need to\s+)?"
    r"(add|implement|create|build|write|make|generate|introduce|extend|support)\s+"
    r"(a|an|the|some|new)\b",
    re.IGNORECASE,
)

#: Research markers: investigation verbs (must pair with an EXTERNAL-thing
#: marker — "look into how the router works" is a repo question, not
#: research).
_RESEARCH_VERBS = re.compile(
    r"\b(research(es|ing)?|investigate|dig into|look into|look up|find out"
    r"|check (the web|online)|search (the web|online))\b",
    re.IGNORECASE,
)
_EXTERNAL_MARKERS = re.compile(
    r"\b(librar(y|ies)|package|framework|approach|online|on the web|internet|"
    r"current(ly)?|latest|alternatives?|compares?|ecosystem|tooling|upstream|"
    r"docs? page|documentation for)\b",
    re.IGNORECASE,
)
_RESEARCH_STRONG = re.compile(
    r"(\bwhat'?s the (best|current|latest)\b"
    r"|\bis there a (librar|package|tool|framework)\b"
    r"|\bwhich (librar|package|framework)\b"
    r"|\bcompare(s|d)?\b.*\b(librar|package|framework|tool)s?\b)",
    re.IGNORECASE,
)

#: Fix-request verbs (change/repair something that exists). "make it
#: stop <symptom>" is the canonical masked-fix shape.
_FIX_VERBS = re.compile(
    r"^\s*(please\s+)?(can you\s+|could you\s+|try to\s+|go\s+|help me\s+)?"
    r"(fix|repair|correct|resolve|patch|make|change|update|refactor|rewrite|remove"
    r"|delete|stop|prevent|handle|ensure|migrate|port|harden|disable|enable)\b",
    re.IGNORECASE,
)

#: Question shapes (checked AFTER fix/build/research so "what's failing?"
#: stays a fix).
_QUESTION_STARTERS = re.compile(
    r"^(how|what|why|where|when|who|which|whom|whose|is|are|do|does|did|"
    r"can|could|should|would|will)\b",
    re.IGNORECASE,
)

_BARE_HELP = re.compile(r"^\s*(help\s+me|help)\s*[.!?]*\s*$", re.IGNORECASE)


@dataclass
class Intent:
    """Outcome of classifying one line of user input.

    kind: one of fix | build | question | research | convo | ambiguous.
    reason: short human-readable justification (trace/audit surface).
    reply: suggested inline reply for convo/ambiguous ("" for work modes).
    used_model: True when the cheap model call decided (gray-zone input).
    """

    kind: str
    reason: str = ""
    reply: str = ""
    used_model: bool = False


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z']+", text.lower())


def _is_question(text: str) -> bool:
    return text.rstrip().endswith("?")


def _bug_artifact_score(text: str) -> Tuple[int, int]:
    """(#bug-language hits, #artifact hits) in the line."""
    return (
        len(_BUG_LANGUAGE.findall(text)),
        len(_ARTIFACT_PATTERN.findall(text)),
    )


def _looks_build(text: str, bug_hits: int) -> bool:
    """True when the line reads like a NEW-capability request.

    Assumes bug_hits is this line's bug-language count: any failure
    language ("make it stop crashing") makes the request a FIX, never a
    build — the failure already exists to be repaired.
    """
    if bug_hits or _is_question(text):
        return False
    if re.search(r"\bnew feature|feature request|new capability\b", text, re.I):
        return True
    if not _BUILD_VERBS.match(text):
        return False
    return bool(_BUILD_OBJECTS.search(text) or _ARTICLE_OBJECT.match(text))


def _looks_research(text: str, bug_hits: int) -> bool:
    """True when the line asks to investigate an EXTERNAL topic (web/docs
    territory), not this repo's behavior. Repo-flavored investigation
    ("look into where config is handled here") is a question; failure-
    flavored ("investigate the crash") is a fix."""
    if bug_hits:
        return False
    if _RESEARCH_STRONG.search(text):
        return True
    return bool(_RESEARCH_VERBS.search(text) and _EXTERNAL_MARKERS.search(text))


def classify_deterministic(text: str) -> Intent:
    """Classify by rules alone; never touches the model.

    Returns kind="ambiguous" with reason="unknown" when NO clear signal
    matched — the caller should then try the model tier (or ask). Assumes
    a single typed line; empty/weird input is ambiguous by construction.
    """
    stripped = (text or "").strip()
    if not stripped:
        return Intent("ambiguous", "empty input")

    low = stripped.lower()
    words = _words(stripped)
    first = words[0] if words else ""

    # -- conversation first: never spend a task on a greeting ------------
    if first in _GREETINGS or " ".join(words[:2]) in _GREETINGS:
        return Intent(
            "convo", "greeting", "hey — describe what's wrong and I'll fix it"
        )
    if low.strip(" .!?") in _THANKS:
        return Intent("convo", "thanks/ack", "anytime")
    if _BARE_HELP.match(stripped):
        return Intent(
            "convo",
            "bare help",
            "describe the bug in a sentence (what fails, where) — or type "
            "help for session commands",
        )
    if any(re.search(p, low) for p in _META_QUESTION_PATTERNS):
        return Intent(
            "convo",
            "meta question about vex",
            "I'm vex — I fix bugs, build features, answer code questions, "
            "and research unfamiliar libraries in this repo, end-to-end and "
            "verified. Type what's wrong, e.g. \"mean() returns the sum; "
            'make it the mean". help lists session commands.',
        )
    if _is_question(stripped) and any(w in low for w in _META_QUESTION_WORDS):
        return Intent(
            "convo",
            "question about the tool itself",
            "that's about me, not the repo — I fix bugs, build features, "
            "and answer questions about this codebase. What's broken?",
        )
    if any(re.search(p, low) for p in _CONVO_PATTERNS):
        return Intent("convo", "chit-chat shape", "ready when you are — what's broken?")

    bug_hits, art_hits = _bug_artifact_score(stripped)

    # -- work modes: research, then build, then fix, then question --------
    if _looks_research(stripped, bug_hits):
        return Intent("research", "investigation verb + external topic marker")
    if _looks_build(stripped, bug_hits):
        return Intent(
            "build", "create-verb + new-capability object, no failure language"
        )

    if bug_hits >= 1:
        return Intent("fix", "failure/symptom language")
    if _FIX_VERBS.match(stripped) and (art_hits or len(words) >= 3):
        return Intent("fix", "fix verb with an object")
    if art_hits >= 1 and _BUILD_VERBS.match(stripped):
        return Intent("fix", "source artifact + change verb")

    if _QUESTION_STARTERS.match(stripped) or _is_question(stripped):
        # A question with no failure language and no build/research marker
        # is a codebase question (or general — Q&A mode answers either).
        return Intent("question", "question shape, no failure language")

    if art_hits >= 1 and len(words) >= 4:
        # Declarative sentence naming source artifacts without a question
        # shape — the canonical "mean() in mathutil.py returns the sum;
        # make it the mean" class. Fix-biased (pinned by wiring tests).
        return Intent("fix", "source artifacts + declarative")

    if "help me" in low and (bug_hits or art_hits):
        return Intent("fix", "help me + failure/artifact signals")

    # -- no clear signal: gray zone --------------------------------------
    return Intent("ambiguous", "unknown")


# ---------------------------------------------------------------------------
# the model tier (gray zone only)
# ---------------------------------------------------------------------------

INTENT_SYSTEM = """\
You are the intent router for Vex, a coding agent that works on ONE repository
(the user's current project). Classify the user's input into exactly one kind:

- "fix"       a bug report: something is broken, failing, wrong, or crashing
              and must be repaired
- "build"     a request for a NEW feature, function, capability, or behavior
              that does not exist yet (nothing is described as broken)
- "question"  asking about the codebase or how something in it works, or a
              general question answerable from repo context (read-only)
- "research"  asking to investigate an unfamiliar library, package,
              approach, or external topic — possibly needing the web
- "convo"     greetings, chit-chat, or questions about Vex itself
- "ambiguous" genuinely unclear — cannot be told apart with confidence

Output STRICTLY this JSON object and nothing else:
{"kind": "<one of the above>", "reason": "<1 short sentence>"}
"""


def _parse_intent_reply(raw: str) -> Optional[str]:
    """Extract a valid kind from the model's reply; None when absent.

    Assumes raw is the model's full text. Tolerates code fences and
    surrounding prose; validates the kind against the six known values.
    """
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        # bare-word fallback: some models answer "build" without JSON
        word = (raw or "").strip().strip("`\"' .!").lower()
        return word if word in _ALL_KINDS else None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    kind = obj.get("kind") if isinstance(obj, dict) else None
    if isinstance(kind, str):
        kind = kind.strip().strip("`\"'").lower()
        if kind in _ALL_KINDS:
            return kind
    return None


def classify_with_model(
    text: str,
    config: Dict[str, Any],
    trace: Optional[Any] = None,
) -> Intent:
    """Classify gray-zone input with ONE cheap model call.

    Assumes config is the task config dict (get_config output or the raw
    task config — only these keys are read: intent_model, model, provider,
    api_key). Passes difficulty_hint="easy" so Terminal 3's adaptive
    routing picks the cheap tier — intent classification never needs the
    expensive models. trace, when given, receives an "intent_model"
    event (kind, reason, ok) for auditability. NEVER raises: any model
    error / unparseable reply / timeout degrades to Intent("ambiguous",
    "model tier failed: <reason>") so the session asks instead of guessing.
    """
    from harness.deps import get_call_model

    reason = "model tier"
    try:
        fn = get_call_model()
        call_cfg = dict(config or {})
        if call_cfg.get("intent_model"):
            call_cfg["model"] = call_cfg["intent_model"]
        messages = [
            {"role": "system", "content": INTENT_SYSTEM},
            {
                "role": "user",
                "content": f"Input to classify:\n{text or '(empty)'}\n\n"
                "Output the JSON verdict now.",
            },
        ]
        started = time.time()
        raw = fn(
            messages,
            difficulty_hint="easy",
            provider=call_cfg.get("provider"),
            model=call_cfg.get("model"),
            api_key=call_cfg.get("api_key"),
        )
        kind = _parse_intent_reply(raw)
        if kind is None:
            reason = "model reply unparseable; asked instead of guessing"
            it = Intent("ambiguous", reason, used_model=True)
        else:
            it = Intent(kind, "model tier", used_model=True)
    except Exception as exc:  # never crash routing over a broken classifier
        it = Intent("ambiguous", f"model tier failed: {exc}", used_model=False)
    if trace is not None:
        try:
            trace.log(
                "intent_model",
                {
                    "kind": it.kind,
                    "reason": it.reason,
                    "used_model": it.used_model,
                    "elapsed_s": round(time.time() - started, 2),
                },
            )
        except Exception:
            pass
    return it


def classify_input(
    text: str,
    config: Optional[Dict[str, Any]] = None,
    trace: Optional[Any] = None,
) -> Intent:
    """Full two-tier classification of one line of user input.

    Tier 1 (deterministic, offline): every clear case — greetings, Vex
    meta questions, bug language, build/research markers, question
    shapes. Tier 2 (ONE cheap model call, difficulty_hint="easy"): only
    the gray zone (deterministic reason "unknown"). Assumes config is a
    plain dict (the session/task config); intent_enabled=False forces the
    legacy behavior (everything is fix). Never raises.
    """
    cfg = config or {}
    if not cfg.get("intent_enabled", True):
        return Intent("fix", "intent_enabled=False (legacy behavior)")
    it = classify_deterministic(text)
    if it.kind != "ambiguous" or it.reason != "unknown":
        if trace is not None:
            try:
                trace.log(
                    "intent", {"kind": it.kind, "reason": it.reason, "tier": "rules"}
                )
            except Exception:
                pass
        return it
    # gray zone: one cheap model call decides
    return classify_with_model(text, cfg, trace)
