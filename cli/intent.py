"""Intent classification for the interactive session (Task E — bug fix).

The defect this fixes: ANY typed line used to launch the fix harness
("hi" started a fix task). The session needs to distinguish three
shapes of input BEFORE spending a task:

- fix request    — describes something broken in the repo (run it)
- conversation   — greetings, questions about vex, chit-chat (answer)
- ambiguous      — not clearly either (ask one clarifying question)

The classifier is deliberately lightweight and deterministic (no model
call, no network): a bug report reads like one — it names a symptom,
a file/function/artifact, or failure language — and a greeting or a
question about the tool itself reads unmistakably different. When the
signals conflict or are absent, we ask instead of guess, because the
cost asymmetry is loud: a wrong fix-run burns minutes and model
budget, a clarifying question costs one line.

Heuristics are data (tables at module scope) so the regression tests
can pin them directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

# --- signal tables -------------------------------------------------------

#: Exact-ish conversational openers (compared on the first word(s),
#: lowercased, punctuation-stripped). "help me" alone is here too: with
#: nothing after it, there is nothing to fix yet.
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

#: Questions ABOUT VEX (the tool), not about the repo's bug. Matched
#: as whole-word questions; the sentence is checked lowercase.
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

#: Sentence SHAPES that are plainly conversational even when not about
#: vex (e.g. "how's it going", "you there?").
_CONVO_PATTERNS = (
    r"how('s| is| are)? (you|it|things|it going|going)",
    r"what'?s up",
    r"you there",
    r"anyone (there|home)",
    r"tell me about yourself",
)

#: Failure/symptom vocabulary — the strongest fix-request signal.
_BUG_LANGUAGE = re.compile(
    r"\b("
    r"bug|error|fail(s|ed|ing|ure)?|crash(es|ed|ing)?|exception|traceback|"
    r"broken|breaks|broke|regression|regressed|flak(e|y)|flaky|"
    r"wrong(ly)?|incorrect(ly)?|inaccurate|off by|inverted|reversed|"
    r"swapped|truncated|duplicated|missing|omitted|ignored|skipped|"
    r"doesn'?t work|does not work|not working|won'?t|will not|can'?t|cannot|"
    r"returns? (the )?wrong|always returns|never returns|unexpected|"
    r"throws?|raised?|panics?|deadlocks?|leaks?|overflows?|underflows?|"
    r"deprecated|typo|miscalculat|off-by-one|edge ?case|fails? when|"
    r"breaks? when|hangs?|times? out|timeout"
    r")\b",
    re.IGNORECASE,
)

#: Source artifacts — a fix request usually names WHERE it hurts.
_ARTIFACT_PATTERN = re.compile(
    r"(\.(py|js|ts|go|rs|java|c|h|cpp|rb|php|sh|yaml|yml|json|toml|md)\b"
    r"|[A-Za-z_][A-Za-z0-9_]{2,}\(\)"  # foo()
    r"|[A-Za-z_][A-Za-z0-9_]{2,}\.py"  # module.py
    r"|test_[a-z0-9_]+|src/|tests?/|lib/|src\\|tests?\\|lib\\"
    r"|[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_]"
    r")",
    re.IGNORECASE,
)

#: Directive verbs that begin real work requests (fix/make/add/...).
_FIX_VERBS = re.compile(
    r"^\s*(please\s+)?(can you\s+|could you\s+|try to\s+|go\s+)?"
    r"(fix|repair|correct|resolve|patch|make|change|update|refactor|"
    r"rewrite|remove|delete|add|implement|support|handle|prevent|"
    r"stop|ensure|write|migrate|port|improve|harden|disable|enable)\b",
    re.IGNORECASE,
)

#: Bare "help me" (nothing after) is conversational, not a fix.
_BARE_HELP = re.compile(r"^\s*(help\s+me|help)\s*[.!?]*\s*$", re.IGNORECASE)


@dataclass
class Intent:
    """Outcome of classifying one line of session input.

    kind: "fix" (run the harness), "convo" (answer, no task),
    or "ambiguous" (ask one clarifying question).
    reply: suggested conversational reply for "convo" (may be "").
    """

    kind: str
    reply: str = ""


def _first_words(text: str, n: int) -> str:
    words = _words(text)
    return " ".join(words[:n])


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z']+", text.lower())


def _is_question(text: str) -> bool:
    return text.rstrip().endswith("?")


def _mentions_meta(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in _META_QUESTION_WORDS)


def _bug_score(text: str) -> Tuple[int, int]:
    """(#bug-language hits, #artifact hits) in the line."""
    return (
        len(_BUG_LANGUAGE.findall(text)),
        len(_ARTIFACT_PATTERN.findall(text)),
    )


def classify(text: str) -> Intent:
    """Classify one session line: fix request, conversation, or ambiguous.

    Assumes a single typed line (the session prompt). Deterministic,
    offline, no exceptions on any input — empty/weird text is ambiguous
    by construction (the session loop already skips blank lines).
    """
    stripped = (text or "").strip()
    if not stripped:
        return Intent("ambiguous")

    low = stripped.lower()
    words = _words(stripped)
    first = _first_words(stripped, 3)
    first2 = _first_words(stripped, 2)
    first1 = words[0] if words else ""

    # bare greetings / thanks / acknowledgements — conversational.
    if first1 in _GREETINGS or first2 in _GREETINGS or first in _GREETINGS:
        return Intent("convo", "hey — describe what's wrong and I'll fix it")
    if low.strip(" .!?") in _THANKS:
        return Intent("convo", "anytime")
    if _BARE_HELP.match(stripped):
        return Intent(
            "convo",
            "describe the bug in a sentence (what fails, where) — "
            "or type help for session commands",
        )

    # meta questions about the tool itself.
    if any(re.search(p, low) for p in _META_QUESTION_PATTERNS):
        return Intent(
            "convo",
            "I'm vex — I fix bugs in this repo end-to-end (plan, edit in a "
            "sandbox, verify with tests). Type what's wrong, e.g. "
            '"mean() returns the sum; make it the mean". help lists '
            "session commands.",
        )
    if _is_question(stripped) and _mentions_meta(stripped):
        return Intent(
            "convo",
            "that's about me, not the repo — I fix bugs described in plain "
            "language. What's broken?",
        )

    # plainly conversational shapes.
    if any(re.search(p, low) for p in _CONVO_PATTERNS):
        return Intent("convo", "ready when you are — what's broken?")

    # question about nothing in particular, no bug signals -> ambiguous.
    bug_hits, art_hits = _bug_score(stripped)

    # explicit fix verbs with an object: "fix the login bug" — strong.
    if _FIX_VERBS.match(stripped) and (bug_hits or art_hits or len(words) >= 3):
        return Intent("fix")

    # bug language or source artifacts: looks like an issue description.
    if bug_hits >= 1:
        return Intent("fix")
    if art_hits >= 1 and len(words) >= 4:
        return Intent("fix")

    # "help me X" with an object X: if X carries fix signals -> fix,
    # else ambiguous (don't launch a task off "help me move apartments").
    if first1 in ("help", "help me") or stripped.lower().startswith("help me "):
        if bug_hits or art_hits:
            return Intent("fix")
        return Intent(
            "ambiguous",
            "is this a bug in the repo you want fixed? describe what fails and where",
        )

    # questions without any bug signal: ambiguous, not a task.
    if _is_question(stripped):
        return Intent(
            "ambiguous",
            "is that about this repo's code? tell me what's failing and "
            "where, and I'll take it from there",
        )

    # declarative sentence, no bug language, no artifacts: ambiguous.
    if len(words) <= 3:
        return Intent("ambiguous", "tell me more — what's wrong, and where?")
    return Intent(
        "ambiguous",
        "not sure that's a bug report — what's failing, and in which file?",
    )


def clarify_reply() -> str:
    """The one-line clarifying question used for ambiguous input."""
    return "is that a bug you want fixed? describe what fails and where"
