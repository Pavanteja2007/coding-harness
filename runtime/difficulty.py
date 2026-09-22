"""Difficulty prediction for a sub-task/step (feeds adaptive model routing).

Two estimators:
  - "heuristic": deterministic feature scoring, v2. Designed after REAL
    ablation data (Round 2, logs/ablations/v1-heuristic): v1 scored the
    raw message text, but Boundary-2 callers send heavily padded prompts
    (system templates, injected file context, constraint re-injection), so
    length/code-block features saturated at "hard" for EVERY call and the
    ON arm degenerated to always-expensive. v2 separates two signals:
      1. INTRINSIC issue signal: scored on the FIRST user message only
         (the issue text — the one input that reflects the task, not the
         prompt scaffolding): stack traces, error-ness, complexity
         keywords (race/deadlock/intermittent/...), file mentions.
      2. STRUGGLE signal: evidence IN the conversation that the CURRENT
         attempt is going badly — repeated failing test output
         ("FAILED"/"AssertionError"/"exit=1") in later user messages,
         syntax errors, multiple failed-submits. Struggle escalates; a
         clean first call never does. This is the harness's failure
         feedback flowing to routing with zero harness changes (the
         verifier's raw output tail already rides the user messages).
  - "llm": asks a cheap model "how hard does this look, 1-5" and maps the
    answer. Falls back to the heuristic on ANY failure (missing key, parse
    error, network) so routing never crashes a task.

Returns a difficulty hint ("easy" | "medium" | "hard") plus the features
and the score, so every routing decision is logged and auditable.

INTERFACES.md Boundary 2 documents difficulty_hint as "easy" | "hard" |
None; "medium" is an added middle tier (Change Log entry filed). Callers
must treat unknown hints as "medium", never crash on them.

Calibration (v2 thresholds) is anchored on the 5 fixture bugs: a plain
one-line bug description scores 0-1 (easy); adding a reproduce recipe or
2+ code mentions lands 2-3 (medium); stack traces + concurrency wording
or clear struggle evidence reach 4+ (hard).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

HINTS = ("easy", "medium", "hard")

# --- intrinsic-issue signals (scored on the FIRST user message) -----------
_STACK_TRACE_RE = re.compile(
    r"traceback \(most recent call last\)|\bexception\b|\berror\b[:\s]|"
    r"nameerror|valueerror|typeerror|indexerror|keyerror|attributeerror",
    re.IGNORECASE,
)
_COMPLEXITY_KEYWORDS = (
    "race",
    "deadlock",
    "flaky",
    "intermittent",
    "concurr",
    "hang",
    "leak",
    "memory",
    "timing",
    "off-by-one",
    "unicode",
    "encoding",
    "regression",
    "interpolate",
    "timezone",
    "utf",
    "crash",
    "reproduce",
)
_PATHISH_RE = re.compile(r"(?:[\w.-]+/){1,4}[\w.-]+\.\w{1,4}")

# --- struggle signals (scored on the LATEST user messages) -----------------
_FAIL_RE = re.compile(
    r"\bFAILED\b|assertionerror|exit=[1-9]\b|\b\d+ failed\b|"
    r"syntaxerror|indentationerror|\btraceback\b",
    re.IGNORECASE,
)
_AMBIGUITY_WORDS = (
    "sometimes",
    "not sure",
    "maybe",
    "unclear",
    "occasionally",
    "sporadic",
)


def _count_unique_matches(text: str, words: Tuple[str, ...]) -> int:
    t = text.lower()
    return sum(1 for w in words if w in t)


def _first_user_content(messages: list) -> str:
    """Content of the first user message (the issue/step description).

    Assumes an OpenAI-style message list; returns "" when there is none.
    """
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def _issue_text_from(first_user: str) -> str:
    """Extract the actual issue/description from a first user message.

    This harness's prompts (harness/prompts.py) merge the issue INTO a
    padded user message: the planner's user message is
    "## Issue\\n{issue}\\n\\n## Retrieved context ...", and step-session
    first messages are feedback blocks ("## Feedback ...") or bare
    "Begin." nudges. Prompt scaffolding must not count as task signal
    (that is exactly the v1 failure), so: cut at '## Retrieved context'
    when present; a message that is itself a '## '-header block (feedback
    / constraints) or a 'Begin.' nudge carries no issue signal -> "".
    """
    if "## Retrieved context" in first_user:
        return first_user.split("## Retrieved context", 1)[0]
    stripped = first_user.lstrip()
    if stripped.startswith("## ") or stripped.startswith("Begin."):
        return ""
    return first_user


def _latest_user_content(messages: list) -> str:
    """Content of the LAST user message (harness failure feedback lands
    there: verifier raw output, syntax errors, constraint re-injection)."""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def heuristic_features(text: str) -> Dict[str, Any]:
    """Extract v2-compat features from a single text blob.

    Kept for direct callers/tests (predict_difficulty now uses
    _features_from_messages on the full message list). Assumes ``text``
    is the issue/step description; scores intrinsic signals only (no
    conversation struggle evidence available from a lone string).
    """
    n = len(text)
    if n < 150:
        length_score = 0
    elif n <= 600:
        length_score = 1
    elif n <= 1500:
        length_score = 2
    else:
        length_score = 3
    code_blocks = min(text.count("```"), 3)
    has_stack_trace = 1 if _STACK_TRACE_RE.search(text) else 0
    file_mentions = min(len(set(_PATHISH_RE.findall(text))), 3)
    complexity_kw = min(_count_unique_matches(text, _COMPLEXITY_KEYWORDS), 3)
    ambiguity = min(_count_unique_matches(text, _AMBIGUITY_WORDS), 2)
    return {
        "char_length": n,
        "length_score": length_score,
        "code_blocks": code_blocks,
        "stack_trace": has_stack_trace,
        "file_mentions": file_mentions,
        "complexity_keywords": complexity_kw,
        "ambiguity": ambiguity,
        "score": (
            length_score
            + code_blocks
            + has_stack_trace
            + file_mentions
            + complexity_kw
            + ambiguity
        ),
    }


def _features_from_messages(messages: list) -> Dict[str, Any]:
    """v2 feature extraction from a full message list.

    Intrinsic signal comes from the FIRST user message's ISSUE portion
    (via _issue_text_from — prompt scaffolding like '## Retrieved context'
    blocks is stripped, else padded prompts saturate the score: the v1
    failure); struggle signal from the LATEST user message (failure
    feedback) plus a count of assistant turns already burned (long
    conversations with no SUBMIT mean the model has been flailing).
    """
    issue = _issue_text_from(_first_user_content(messages))
    latest = _latest_user_content(messages)
    assistant_turns = sum(
        1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant"
    )

    # -- intrinsic (issue-only; cap each dimension) --
    has_error = 1 if _STACK_TRACE_RE.search(issue) else 0
    complexity_kw = min(_count_unique_matches(issue, _COMPLEXITY_KEYWORDS), 3)
    file_mentions = min(len(set(_PATHISH_RE.findall(issue))), 2)
    ambiguity = min(_count_unique_matches(issue, _AMBIGUITY_WORDS), 2)
    # A plain one-liner scores 0; a detailed recipe nudges up, not the
    # 3-point v1 length saturation (real prompts are big by construction).
    length_score = 1 if len(issue) > 400 else 0

    intrinsic = has_error * 2 + complexity_kw + file_mentions + ambiguity + length_score

    # -- struggle (conversation evidence) --
    fail_hits = len(_FAIL_RE.findall(latest))
    struggle = 0
    if fail_hits:
        struggle += min(fail_hits, 3)
    if re.search(r"syntaxerror|indentationerror", latest, re.IGNORECASE):
        struggle += 1
    if assistant_turns >= 4:
        struggle += 1  # this step has already burned many turns

    return {
        "issue_chars": len(issue),
        "issue_has_error": has_error,
        "issue_complexity_keywords": complexity_kw,
        "issue_file_mentions": file_mentions,
        "issue_ambiguity": ambiguity,
        "intrinsic_score": intrinsic,
        "struggle_fails": min(fail_hits, 3),
        "assistant_turns": assistant_turns,
        "struggle_score": struggle,
        "score": intrinsic + struggle,
    }


# Scaffolding markers used by this harness's prompts (see
# _issue_text_from): a first-user message that is one of these blocks
# carries no issue signal. Kept here so prompt changes in harness/
# prompts.py have ONE place to be mirrored in the predictor.
_SCAFFOLD_MARKERS = (
    "## Retrieved context",
    "## Constraints",
    "## Feedback",
)


def score_to_hint(score: int) -> str:
    """Map a v2 feature score to a difficulty hint.

    Thresholds calibrated on the 5 fixture bugs (plain bug text -> easy;
    error/reproduce wording -> medium; stack traces + concurrency wording
    or live struggle evidence -> hard). An offline calibration file
    (runtime/difficulty_calibration.json, written by the analyze-history
    maintenance job after a HELD-OUT-validated improvement) overrides
    the built-in bands; absent/unreadable/malformed file -> built-ins.
    """
    b = _calibration_bands()
    if b is not None:
        if score <= b[0]:
            return "easy"
        if score < b[1]:
            return "medium"
        return "hard"
    if score <= 1:
        return "easy"
    if score <= 3:
        return "medium"
    return "hard"


# Built-in v2 bands (easy if <=1, medium if <=3, else hard) — the
# documented defaults; the offline calibration file overrides these.
_BUILTIN_BANDS = (1, 4)
_CALIBRATION_FILE = Path(__file__).with_name("difficulty_calibration.json")
_cal_cache: Optional[tuple] = None
_cal_cache_mtime: Optional[float] = None


def _calibration_bands() -> Optional[tuple]:
    """(easy_max, hard_min) from the calibration file, or None.

    Assumes the file (if present) is the analyze-history job's artifact
    ({"easy_max": int, "hard_min": int, ...}); any missing/invalid/
    out-of-range content degrades to the built-in bands. Cached with
    mtime invalidation so per-call reads stay cheap in the router's
    hot path. Never raises.
    """
    global _cal_cache, _cal_cache_mtime
    try:
        if not _CALIBRATION_FILE.exists():
            return None
        mtime = _CALIBRATION_FILE.stat().st_mtime
        if _cal_cache is not None and mtime == _cal_cache_mtime:
            return _cal_cache
        data = json.loads(_CALIBRATION_FILE.read_text(encoding="utf-8"))
        easy_max = int(data["easy_max"])
        hard_min = int(data["hard_min"])
        if 0 <= easy_max < hard_min <= 12:
            _cal_cache = (easy_max, hard_min)
            _cal_cache_mtime = mtime
            return _cal_cache
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def active_bands() -> tuple:
    """The bands score_to_hint is currently using (for tests/diagnostics)."""
    b = _calibration_bands()
    return b if b is not None else _BUILTIN_BANDS


def predict_difficulty(
    text: str,
    estimator: str = "heuristic",
    llm_cfg: Optional[Dict[str, Any]] = None,
    messages: Optional[list] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Predict difficulty for one step/sub-task.

    Assumes ``text`` is the step or issue description to classify and
    ``estimator`` is one of "heuristic" | "llm" | "off" ("off" behaves as
    "heuristic" here; the router treats "off" as no routing). When the
    caller has the full message list (the router always does), pass it
    via ``messages``: v2 scores intrinsic issue signal from the first
    user message plus struggle evidence from the conversation tail —
    plain ``text`` alone falls back to intrinsic-only features (v1-compat
    shape). Returns (hint, info) where info carries the features/score
    (and, for the llm estimator, the raw reply) for logging.
    """
    if messages:
        feats = _features_from_messages(messages)
        hint = score_to_hint(feats["score"])
        base_hint = hint  # heuristic fallback target for the llm arm
    else:
        feats = heuristic_features(text)
        hint = score_to_hint(feats["score"])
        base_hint = hint
    if estimator != "llm":
        return hint, {"estimator": estimator, "features": feats}

    info: Dict[str, Any] = {"estimator": "llm", "features": feats, "llm_hint": None}
    try:
        from .model_router import call_model

        cfg = llm_cfg or {}
        reply = call_model(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You rate how difficult a coding sub-task looks for an "
                        "AI coding agent. Reply with ONLY a single integer 1-5, "
                        "where 1 is trivial and 5 is very hard."
                    ),
                },
                {"role": "user", "content": text[:4000]},
            ],
            provider=cfg.get("provider"),
            model=cfg.get("model"),
            api_key=cfg.get("api_key"),
        )
        m = re.search(r"[1-5]", reply or "")
        if not m:
            raise ValueError(f"no 1-5 rating in reply: {reply!r:.120}")
        rating = int(m.group(0))
        llm_hint = "easy" if rating <= 2 else ("medium" if rating <= 4 else "hard")
        info["llm_hint"] = llm_hint
        info["llm_rating"] = rating
        return llm_hint, info
    except Exception as exc:
        info["llm_fallback_reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return base_hint, info
