"""Difficulty prediction for a sub-task/step (feeds adaptive model routing).

Three estimators:
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
  - "structural" (R2-13): REPOSITORY-shape features instead of issue-text
    keywords — module/test counts, test-suite density, the bug class, the
    FAN-IN of the touched symbols, and whether the declared target test
    exists. This is the replacement signal the held-out comparison in
    :func:`compare_predictors` was built to judge, and it is NOT the
    default: it ships only if it beats the heuristic on held-out data.
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

R2-13 honesty note, and it governs the whole structural section: the v2
features FAILED their own calibration (runtime/AGENTS.md, the
Cross-Task-Learning round: held-out delta zero, "the current feature set
does not separate easy from hard on this data"). The structural features
below are the response to that finding, and they are subject to the SAME
discipline: :func:`compare_predictors` decides on held-out data whether
they beat the incumbent, and a predictor that does not beat it is
reported as losing and is not enabled. Nothing in this module turns the
structural estimator on by itself.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

HINTS = ("easy", "medium", "hard")

# --- intrinsic-issue signals (scored on the FIRST user message) -----------
_STACK_TRACE_RE = re.compile(
    r"traceback \(most recent call last\)|\bexception\b|\berror\b[:\s]|"
    r"nameerror|valueerror|typeerror|indexerror|keyerror|attributeerror|"
    r"importerror|modulenotfounderror",
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
    """Count whole keyword occurrences, excluding incidental substrings."""
    lowered = text.lower()
    return sum(
        1
        for word in words
        if re.search(rf"(?<!\w){re.escape(word.lower())}(?!\w)", lowered)
    )


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


def _latest_struggle_content(messages: list) -> str:
    """Return feedback after the first user turn, excluding prompt padding."""
    user_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    if len(user_indexes) < 2:
        return ""
    latest = str(messages[user_indexes[-1]].get("content", ""))
    if "## Retrieved context" in latest:
        latest = latest.split("## Retrieved context", 1)[0]
    return latest


def routing_text(messages: list) -> str:
    """Return only intrinsic issue and genuine struggle text for estimation."""
    issue = _issue_text_from(_first_user_content(messages))
    struggle = _latest_struggle_content(messages)
    return "\n".join(part for part in (issue, struggle) if part)


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
    latest = _latest_struggle_content(messages)
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
    """Return the bands score_to_hint is currently using."""
    bands = _calibration_bands()
    return bands if bands is not None else _BUILTIN_BANDS


def calibration_path() -> Path:
    """Return the module-adjacent optional calibration artifact path."""
    return _CALIBRATION_FILE


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
        classification_text = routing_text(messages)
        hint = score_to_hint(feats["score"])
        base_hint = hint
    else:
        feats = heuristic_features(text)
        classification_text = text
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
                {"role": "user", "content": classification_text[:4000]},
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


# ---------------------------------------------------------------------------
# R2-13 — STRUCTURAL difficulty features
#
# These replace the LEXICAL question ("does the issue text use scary words?")
# with the question that actually predicts cost: how big is the blast radius of
# this change? Everything below is offline, deterministic, and bounded — a
# predictor that costs more than the routing decision it informs is not a
# predictor, it is latency. Every bound that fires is REPORTED in the receipt
# (``*_truncated`` / ``walk_incomplete``) rather than silently producing a
# smaller number than the truth.
# ---------------------------------------------------------------------------

#: Directories a structural walk never descends into. A bare ``os.walk`` of a
#: real checkout (measured on THIS repo during R2-10) visits 291k files in
#: 362s, so an unbounded walk is not a slow feature extraction, it is a hang.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "dist",
        "build",
        "logs",
        ".eggs",
        "htmlcov",
        "coverage",
    }
)

#: Bounded walk budgets. A repository that exceeds them is reported as
#: ``walk_incomplete`` and its counts are a LOWER BOUND, which the score
#: treats as such (a truncated count cannot manufacture confidence).
MAX_WALK_ENTRIES = 4_000
MAX_FANIN_MODULES = 600

#: Closed bug-class vocabulary. The weights are DECLARED priors from a bug
#: taxonomy, not fitted parameters — declaring them is what makes the
#: held-out comparison meaningful rather than a curve fit to the answer.
BUG_CLASSES = (
    "boundary",
    "operator",
    "constant",
    "guard",
    "typing",
    "concurrency",
    "state",
    "io",
    "import",
    "api",
    "unknown",
)
BUG_CLASS_WEIGHTS: Dict[str, int] = {
    "unknown": 0,
    "constant": 0,
    "operator": 0,
    "boundary": 1,
    "import": 1,
    "guard": 1,
    "typing": 1,
    "io": 1,
    "api": 1,
    "state": 2,
    "concurrency": 2,
}

#: Ordered (class -> pattern) probes. First match wins, so the list order IS
#: the precedence and is part of the contract; "unknown" is the fallback.
_BUG_CLASS_PROBES: Tuple[Tuple[str, str], ...] = (
    (
        "concurrency",
        r"\b(race|deadlock|flaky|intermittent|thread|async|concurren|lock|"
        r"starv|livelock|non-?determin)",
    ),
    (
        "state",
        r"\b(mutable|shared state|cache|stale|leak|global|re-?entrant|"
        r"idempot|order-?dependent|reuse)",
    ),
    (
        "io",
        r"\b(file|path|encoding|unicode|newline|truncat|permission|"
        r"directory|filesystem|serial|deserial|read|write)",
    ),
    (
        "import",
        r"\b(import|module not found|circular|namespace|__init__|package)\b",
    ),
    (
        "typing",
        r"\b(type|annotation|None|none|optional|generic|mypy|signature|"
        r"return type|kwarg)",
    ),
    (
        "guard",
        r"\b(guard|except|try|error handling|raises|unhandled|swallow|"
        r"silently|lost|missing)",
    ),
    (
        "boundary",
        r"\b(off-?by-?one|boundary|edge case|index|slice|range|inclusive|"
        r"exclusive|empty|first|last)",
    ),
    (
        "operator",
        r"\b(operator|wrong operator|instead of|should be|comparison|"
        r"precedence|assign)",
    ),
    (
        "api",
        r"\b(api|signature|contract|deprecat|return|argument|parameter|"
        r"keyword argument)\b",
    ),
    (
        "constant",
        r"\b(constant|literal|hard-?coded|magic number|typo|value)\b",
    ),
)


def classify_bug_class(issue_text: str) -> str:
    """Return the closed bug class for ``issue_text`` (``"unknown"`` default).

    Assumes a non-empty or empty issue string; an empty or non-string input is
    ``"unknown"`` rather than a guess. The probes are ordered and the FIRST
    match wins, so the precedence in :data:`_BUG_CLASS_PROBES` is part of the
    contract — reordering it changes predictions.
    """
    text = str(issue_text or "")
    if not text.strip():
        return "unknown"
    lowered = text.casefold()
    for name, pattern in _BUG_CLASS_PROBES:
        if re.search(pattern, lowered):
            return name
    return "unknown"


def _iter_source_files(root: Path, *, budget: int) -> Tuple[List[Path], bool]:
    """Walk ``root`` for ``.py`` files, pruning generated/vendor trees.

    Returns ``(files, complete)`` where ``complete`` is False when the entry
    budget fired. ``files`` is sorted so every derived count is deterministic
    across runs and machines.
    """
    files: List[Path] = []
    truncated = False
    for dirpath, dirnames, filenames in __import__("os").walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            if len(files) >= budget:
                return files, True
            files.append(Path(dirpath) / name)
    return files, truncated


def _module_name(path: Path, root: Path) -> str:
    """Return a module's dotted name relative to ``root`` (``__init__`` aware)."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return path.stem
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = path.stem
    return ".".join(parts)


def repo_shape(repo_root: Any, *, budget: int = MAX_WALK_ENTRIES) -> Dict[str, Any]:
    """Measure a repository's shape: modules, source lines, test-suite density.

    Assumes ``repo_root`` is a directory of Python source. A missing directory
    returns ``measured=False`` with zeroed counts — an absent repository is
    UNKNOWN, not an empty one, and the two must not be reported identically.
    The walk prunes vendor/generated trees and is capped at ``budget`` files;
    when the cap fires, ``walk_incomplete`` is True and every count below is a
    lower bound.
    """
    empty = {
        "measured": False,
        "repo_root": str(repo_root or ""),
        "modules": 0,
        "source_lines": 0,
        "test_files": 0,
        "test_functions": 0,
        "tests_per_module": None,
        "walk_incomplete": False,
        "reason": "repo_root_absent",
    }
    if not repo_root:
        return {**empty, "reason": "repo_root_absent"}
    root = Path(str(repo_root))
    if not root.is_dir():
        return {**empty, "repo_root": str(root), "reason": "repo_root_absent"}
    files, complete = _iter_source_files(root, budget=budget)
    modules = 0
    source_lines = 0
    test_files = 0
    test_functions = 0
    for path in files:
        relative_parts = path.relative_to(root).parts
        is_test = "tests" in relative_parts or path.name.startswith("test_")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        modules += 1
        lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        source_lines += lines
        if not is_test:
            continue
        test_files += 1
        test_functions += len(
            re.findall(r"^\s*(?:async\s+)?def\s+test\w*", text, re.MULTILINE)
        )
    density = (test_functions / modules) if modules else None
    return {
        "measured": True,
        "repo_root": str(root),
        "modules": modules,
        "source_lines": source_lines,
        "test_files": test_files,
        "test_functions": test_functions,
        "tests_per_module": round(density, 4) if density is not None else None,
        "walk_incomplete": complete,
        "reason": "walk_budget_exhausted" if complete else "measured",
    }


def _import_graph(
    root: Path, files: Sequence[Path], *, budget: int = MAX_FANIN_MODULES
) -> Tuple[Dict[str, List[str]], bool]:
    """Return ``(importers_of_module, complete)`` from a stdlib-ast import scan.

    Assumes ``files`` came from :func:`_iter_source_files`. A file that does not
    parse is SKIPPED (an unparseable module has no trustworthy imports) and
    the skip is not silently folded into a zero fan-in: the count for a symbol
    only ever grows with the evidence found, and ``complete`` is False when the
    module budget fired so a caller can refuse to trust a bounded answer.
    """
    importers: Dict[str, List[str]] = {}
    complete = True
    for path in files[: max(1, budget)]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        module = _module_name(path, root)
        for node in ast.walk(tree):
            names: List[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # relative import: not a global fan-in signal
                base = node.module or ""
                if not base:
                    continue
                # `from M import x` imports M. Recording only ``M.x`` would
                # make a module that imports a NAME look like it does not
                # import the module, which under-counts every function's
                # fan-in by exactly the from-import half of the codebase.
                names = [base]
                names.extend(f"{base}.{alias.name}" for alias in node.names)
            for name in names:
                importers.setdefault(name, []).append(module)
    if len(files) > max(1, budget):
        complete = False
    for key in importers:
        importers[key] = sorted(set(importers[key]))
    return importers, complete


def symbol_fan_in(
    repo_root: Any,
    symbols: Sequence[str],
    *,
    budget: int = MAX_FANIN_MODULES,
) -> Dict[str, Any]:
    """Measure the FAN-IN of ``symbols``: how many modules reference each one.

    Assumes ``symbols`` are bare or dotted symbol names and ``repo_root`` is a
    Python source tree. Fan-in is counted as "distinct modules that import a
    module defining the symbol AND mention that symbol", which is the blast
    radius a one-line change actually carries. Returns a receipt with
    ``fan_in`` per symbol, ``max``/``mean``, and ``truncated``; a bounded scan
    reports ``truncated=True`` and its numbers are a LOWER BOUND.
    """
    wanted = [str(name).strip() for name in symbols or [] if str(name).strip()]
    if not wanted or not repo_root:
        return {
            "fan_in": {name: 0 for name in wanted},
            "max": 0,
            "mean": 0.0,
            "truncated": False,
            "measured": False,
            "reason": "no_symbols" if not wanted else "repo_root_absent",
        }
    root = Path(str(repo_root))
    if not root.is_dir():
        return {
            "fan_in": {name: 0 for name in wanted},
            "max": 0,
            "mean": 0.0,
            "truncated": False,
            "measured": False,
            "reason": "repo_root_absent",
        }
    files, walk_complete = _iter_source_files(root, budget=MAX_WALK_ENTRIES)
    importers, graph_complete = _import_graph(root, files, budget=budget)
    # Which modules DEFINE each symbol, so "imports a module that defines it"
    # is a real reachability claim rather than a name coincidence.
    definitions: Dict[str, set] = {name: set() for name in wanted}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        module = _module_name(path, root)
        for node in tree.body:
            names: List[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            for name in names:
                for wanted_name in wanted:
                    if wanted_name == name or wanted_name.endswith("." + name):
                        definitions[wanted_name].add(module)
    fan_in: Dict[str, int] = {}
    for wanted_name in wanted:
        reach: set = set()
        for module in definitions[wanted_name]:
            reach.update(importers.get(module, ()))
        fan_in[wanted_name] = len(reach)
    values = [fan_in[name] for name in wanted]
    return {
        "fan_in": fan_in,
        "max": max(values) if values else 0,
        "mean": round(sum(values) / len(values), 4) if values else 0.0,
        "truncated": bool(walk_complete or graph_complete),
        "measured": True,
        "reason": "bounded_scan" if (walk_complete or graph_complete) else "measured",
    }


def target_test_exists(repo_root: Any, target_test: Any) -> Optional[bool]:
    """Whether the declared target test resolves on disk.

    Assumes ``target_test`` is a pytest-style selector (``path``, ``path::name``
    or ``path::Class::name``) or a bare module path. Returns ``None`` — never
    ``False`` — when the target is ABSENT FROM THE TASK, because "the operator
    declared no target" and "the declared target is not in this repository" are
    different facts and the first is not evidence of the second. A path that
    exists but whose node id cannot be checked returns ``True`` (the file is
    there) with the limitation left to the caller, because this is a feature,
    not the verifier.
    """
    if target_test is None or str(target_test).strip() == "":
        return None
    if not repo_root:
        return None
    root = Path(str(repo_root))
    selector = str(target_test).strip().replace("\\", "/")
    path_part = selector.split("::", 1)[0]
    if not path_part:
        return None
    candidate = Path(path_part)
    if not candidate.is_absolute():
        candidate = root / path_part
    if candidate.is_file():
        return True
    # A pytest selector may name the file without its suffix.
    for suffix in (".py", ""):
        probe = Path(str(candidate) + suffix)
        if probe.is_file():
            return True
    return False


#: Structural weights, all capped, summed into an integer score. Declared, not
#: fitted: :func:`compare_predictors` is what decides whether these help.
_STRUCTURAL_WEIGHTS: Dict[str, Tuple[int, int]] = {
    # feature -> (weight, cap)
    "repo_size": (1, 2),
    "test_density": (1, 2),
    "target_test_missing": (1, 2),
    "fan_in": (1, 3),
    "bug_class": (1, 2),
    "multi_module": (1, 2),
}


def _bucket(value: Optional[float], thresholds: Sequence[float]) -> int:
    """Return the index of the first threshold ``value`` reaches (capped).

    Assumes ``thresholds`` is ascending. A ``None`` value returns 0 because an
    unmeasured feature contributes nothing and is reported as unmeasured, not
    as "good" or "bad".
    """
    if value is None:
        return 0
    for index, threshold in enumerate(thresholds):
        if value >= threshold:
            return index + 1
    return 0


def structural_features(
    context: Optional[Mapping[str, Any]] = None,
    *,
    walk_budget: int = MAX_WALK_ENTRIES,
) -> Dict[str, Any]:
    """Extract the R2-13 structural features for one task.

    Assumes ``context`` may carry ``repo_path`` (a directory), ``issue_text``,
    ``target_test`` (a pytest selector or absent) and ``changed_files``
    (the files the task expects to touch). Every input is optional: a missing
    repository yields ``shape.measured is False`` and a
    ``feature_coverage`` receipt that says which features could not be
    resolved, so a comparison over this function can report the coverage it
    actually had instead of silently scoring absent features as zero.
    """
    context = dict(context or {})
    repo_path = context.get("repo_path") or context.get("repo")
    issue_text = str(context.get("issue_text") or context.get("text") or "")
    target_test = context.get("target_test")
    changed = [
        str(name) for name in (context.get("changed_files") or ()) if str(name).strip()
    ]
    shape = repo_shape(repo_path, budget=walk_budget)
    symbol_names: List[str] = []
    for entry in context.get("touched_symbols") or ():
        name = str(entry).strip()
        if name and "::" in name:
            name = name.split("::", 1)[1]
        if name:
            symbol_names.append(name)
    fan_in = (
        symbol_fan_in(repo_path, symbol_names)
        if symbol_names
        else {
            "fan_in": {},
            "max": 0,
            "mean": 0.0,
            "truncated": False,
            "measured": False,
            "reason": "no_symbols",
        }
    )
    bug_class = classify_bug_class(issue_text)
    target_present = target_test_exists(repo_path, target_test)
    modules = shape["modules"] if shape["measured"] else None
    tests_per_module = shape["tests_per_module"] if shape["measured"] else None
    parts: Dict[str, int] = {
        "repo_size": _bucket(modules, (25, 150)),
        "test_density": _bucket(tests_per_module, (0.5, 2.0)),
        # A MISSING declared target test is the signal. An UNDECLARED one is
        # not evidence of anything, so it contributes 0 and is reported in
        # feature_coverage rather than scored.
        "target_test_missing": 2 if target_present is False else 0,
        "fan_in": _bucket(fan_in["max"] if fan_in["measured"] else None, (2, 8)),
        "bug_class": min(BUG_CLASS_WEIGHTS.get(bug_class, 0), 2),
        "multi_module": 2 if len({_module_suffix(p) for p in changed}) > 1 else 0,
    }
    score = 0
    contributions: Dict[str, int] = {}
    for name, (weight, cap) in _STRUCTURAL_WEIGHTS.items():
        contribution = min(int(parts.get(name, 0)), cap) * weight
        contributions[name] = contribution
        score += contribution
    resolved = {
        "repo_size": modules is not None,
        "test_density": tests_per_module is not None,
        "target_test_missing": target_present is not None,
        "fan_in": bool(fan_in["measured"]),
        "bug_class": bool(issue_text.strip()),
        "multi_module": bool(changed),
    }
    return {
        "estimator": "structural",
        "repo_shape": shape,
        "fan_in": fan_in,
        "bug_class": bug_class,
        "bug_class_weight": BUG_CLASS_WEIGHTS.get(bug_class, 0),
        "target_test_declared": target_test is not None
        and str(target_test).strip() != "",
        "target_test_present": target_present,
        "changed_files": len(changed),
        "distinct_touched_modules": len({_module_suffix(p) for p in changed}),
        "parts": parts,
        "contributions": contributions,
        "feature_coverage": resolved,
        "resolved_feature_count": sum(1 for value in resolved.values() if value),
        "feature_count": len(resolved),
        "score": score,
    }


def _module_suffix(rel_path: str) -> str:
    """Return a path's containing directory, used to count touched modules."""
    normalized = str(rel_path or "").replace("\\", "/")
    return normalized.rsplit("/", 1)[0] if "/" in normalized else ""


#: Built-in structural bands, chosen from the feature CAPS so the mapping is
#: readable: a score of 0-1 is a one-module, declared-test, zero-fan-in change;
#: 8+ needs a big repo AND broad fan-in AND a hard bug class at once.
_BUILTIN_STRUCTURAL_BANDS = (2, 6)


def score_to_structural_hint(
    score: int, bands: Optional[Tuple[int, int]] = None
) -> str:
    """Map a structural score to a difficulty hint.

    Assumes ``score`` is a non-negative integer and ``bands`` is
    ``(easy_max, hard_min)`` or ``None`` for the built-in bands. Unlike the v2
    bands this is NOT overridable by the calibration artifact: that file records
    a held-out-validated refit of the LEXICAL features, and silently applying
    it to a different feature family would be a claim about a model that was
    never fitted.
    """
    easy_max, hard_min = bands or _BUILTIN_STRUCTURAL_BANDS
    if score <= easy_max:
        return "easy"
    if score < hard_min:
        return "medium"
    return "hard"


def predict_structural(
    context: Optional[Mapping[str, Any]] = None,
    *,
    bands: Optional[Tuple[int, int]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Predict difficulty from REPOSITORY SHAPE (the R2-13 challenger).

    Assumes the same context shape as :func:`structural_features`. Returns
    ``(hint, info)`` in the same shape as :func:`predict_difficulty` so the two
    predictors are directly comparable on the same rows.

    This function is the CHALLENGER, not the default. It becomes the routing
    predictor only when a caller names it (``difficulty_features="structural"``)
    or a held-out-validated calibration exists — and no such calibration ships
    unless :func:`compare_predictors` says it earned one.
    """
    features = structural_features(context)
    hint = score_to_structural_hint(features["score"], bands)
    return hint, {"estimator": "structural", "features": features}


def group_holdout_split(
    rows: Sequence[Mapping[str, Any]], frac: float = 0.25, seed: int = 7
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split whole bug GROUPS into deterministic train/holdout row lists.

    Assumes each row carries a ``group_id``. A group NEVER straddles the split:
    the same bug re-run across ablation windows is one underlying observation,
    and letting it appear in both halves would let the evaluation see its own
    answer. Ordering is a SHA-256 of ``seed:group`` so the split is identical
    on every machine and every run.
    """
    frac = float(frac)
    if not 0 < frac < 1:
        raise ValueError("holdout fraction must be between 0 and 1")
    by_group: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows or ():
        by_group.setdefault(str(row.get("group_id") or row.get("task_id")), []).append(
            row
        )
    if not by_group:
        return [], []
    ranked = sorted(
        by_group,
        key=lambda group: hashlib.sha256(
            f"{int(seed)}:{group}".encode("utf-8", "replace")
        ).hexdigest(),
    )
    cut = max(1, round(len(ranked) * frac))
    holdout_groups = set(ranked[:cut])
    train = [
        dict(row) for row in rows if str(row.get("group_id")) not in holdout_groups
    ]
    holdout = [dict(row) for row in rows if str(row.get("group_id")) in holdout_groups]
    return train, holdout


_HINT_NUM = {"easy": 0, "medium": 1, "hard": 2}

#: A difficulty predictor is only validated by observations of the thing it
#: predicts. One hard-labelled held-out row can be "won" by a single lucky
#: prediction, so shipping is gated on a DECLARED minimum rather than on
#: "at least one". This is a floor for honesty, not a tuned parameter: no
#: measurement can move it, only more data can clear it.
MIN_HOLDOUT_HARD_LABELS = 3


def _score_hints(
    rs: Sequence[Mapping[str, Any]], key: str, name: str
) -> Dict[str, Any]:
    """Score one predictor's hints against the labels on ``rs``.

    A ``hard``-labelled row predicted easy/medium is a MISSED escalation (the
    expensive answer arrived too late or not at all); an ``easy``-labelled row
    predicted hard is a FALSE escalation (money spent for nothing). Those two
    counts are what routing economics actually care about, so they are reported
    alongside plain accuracy.
    """
    n = len(rs)
    if not n:
        return {"name": name, "n": 0}
    missed = [
        r for r in rs if r.get("label") == "hard" and r.get(key) in ("easy", "medium")
    ]
    false_esc = [r for r in rs if r.get("label") == "easy" and r.get(key) == "hard"]
    correct = [
        r
        for r in rs
        if (
            (r.get("label") == "easy" and r.get(key) in ("easy", "medium"))
            or (r.get("label") == "hard" and r.get(key) == "hard")
        )
    ]
    ordinal_errors = [
        abs(_HINT_NUM.get(str(r.get(key)), 1) - (2 if r.get("label") == "hard" else 0))
        for r in rs
    ]
    return {
        "name": name,
        "n": n,
        "accuracy_easy_or_hard": round(len(correct) / n, 4),
        "missed_escalations": len(missed),
        "false_escalations": len(false_esc),
        "missed_group_ids": sorted({str(r.get("group_id")) for r in missed})[:20],
        "false_group_ids": sorted({str(r.get("group_id")) for r in false_esc})[:20],
        "mean_ordinal_error": round(sum(ordinal_errors) / n, 4),
    }


def compare_predictors(
    rows: Sequence[Mapping[str, Any]],
    *,
    holdout_frac: float = 0.25,
    seed: int = 7,
    legacy_key: str = "legacy_hint",
    challenger_key: str = "structural_hint",
) -> Dict[str, Any]:
    """Compare the incumbent predictor against the structural challenger.

    Assumes each row carries ``group_id``, ``label`` (``"easy"``/``"hard"``),
    a hint under ``legacy_key`` and a hint under ``challenger_key``. The
    verdict is decided on the HELD-OUT groups only, because a challenger that
    wins on the data it was read from has not won anything.

    The report is deliberately hard to misread:

    * ``winner`` is one of ``"legacy"`` / ``"structural"`` / ``"tie"``, computed
      from held-out missed escalations first, then false escalations, then
      accuracy;
    * ``decidable`` is True when the holdout contains at least one hard label
      at all. A holdout with none cannot validate a difficulty predictor in
      principle, not merely in this sample;
    * ``sample_adequate`` is True when the holdout carries at least
      :data:`MIN_HOLDOUT_HARD_LABELS` hard labels — the declared floor below
      which a held-out "win" is a single observation and cannot justify
      replacing a shipped predictor;
    * ``ship`` is True ONLY when the structural predictor won AND the sample
      is adequate. Winning on one hard row is reported as a WIN and refused as
      a shipping decision, because those are different claims;
    * ``coverage`` reports how many rows had their structural features
      resolved, because a predictor scoring absent features as zero is being
      judged on a handicap it did not choose.

    This function decides and reports. It does not enable anything.
    """
    ordered = [dict(row) for row in rows or ()]
    train, holdout = group_holdout_split(ordered, holdout_frac, seed)
    if not ordered:
        return {
            "rows": 0,
            "winner": "none",
            "ship": False,
            "decidable": False,
            "reason": "no_observations",
            "honesty": "no rows were available; nothing was compared",
        }
    scores = {
        "train": {
            "legacy": _score_hints(train, legacy_key, "legacy"),
            "structural": _score_hints(train, challenger_key, "structural"),
        },
        "holdout": {
            "legacy": _score_hints(holdout, legacy_key, "legacy"),
            "structural": _score_hints(holdout, challenger_key, "structural"),
        },
    }
    holdout_hard = sum(1 for row in holdout if row.get("label") == "hard")
    holdout_n = len(holdout)
    resolved = [row for row in ordered if row.get("features_resolved")]
    coverage = {
        "rows": len(ordered),
        "features_resolved": len(resolved),
        "features_resolved_frac": round(len(resolved) / len(ordered), 4)
        if ordered
        else 0.0,
    }
    if holdout_n == 0:
        return {
            "rows": len(ordered),
            "winner": "none",
            "ship": False,
            "decidable": False,
            "sample_adequate": False,
            "reason": "empty_holdout",
            "scores": scores,
            "coverage": coverage,
        }
    decidable = holdout_hard > 0
    sample_adequate = holdout_hard >= MIN_HOLDOUT_HARD_LABELS

    def _key(result: Dict[str, Any]) -> Tuple[int, int, float]:
        return (
            int(result.get("missed_escalations", 0)),
            int(result.get("false_escalations", 0)),
            -float(result.get("accuracy_easy_or_hard", 0.0)),
        )

    legacy = scores["holdout"]["legacy"]
    structural = scores["holdout"]["structural"]
    if _key(legacy) < _key(structural):
        winner = "legacy"
    elif _key(structural) < _key(legacy):
        winner = "structural"
    else:
        winner = "tie"
    ship = bool(decidable and sample_adequate and winner == "structural")
    if not decidable:
        honesty = (
            "the held-out split contains no hard-labelled observation, so a "
            "difficulty predictor cannot be validated on it; the incumbent "
            "stays the default"
        )
    elif winner == "structural" and not sample_adequate:
        honesty = (
            f"the structural predictor beat the incumbent on the held-out "
            f"groups, but that split carries only {holdout_hard} hard-labelled "
            f"observation(s) against a declared floor of "
            f"{MIN_HOLDOUT_HARD_LABELS}. A win on that few hard cases is not "
            f"evidence enough to replace a shipped predictor, so it is NOT "
            f"shipped: the incumbent stays the default and the result is "
            f"recorded as promising-but-unproven."
        )
    elif winner == "structural":
        honesty = (
            "the structural predictor beat the incumbent on held-out groups "
            "with an adequate hard-label count and is eligible to be enabled"
        )
    elif winner == "legacy":
        honesty = (
            "the structural predictor did NOT beat the incumbent on held-out "
            "groups, so it is not shipped and the incumbent stays the default"
        )
    else:
        honesty = (
            "neither predictor beat the other on held-out groups; the tie is "
            "not a win, so the incumbent stays the default"
        )
    return {
        "rows": len(ordered),
        "train_rows": len(train),
        "holdout_rows": holdout_n,
        "holdout_groups": len({str(r.get("group_id")) for r in holdout}),
        "holdout_hard_labels": holdout_hard,
        "min_holdout_hard_labels": MIN_HOLDOUT_HARD_LABELS,
        "decidable": decidable,
        "sample_adequate": sample_adequate,
        "winner": winner,
        "ship": ship,
        "reason": "held_out_comparison",
        "scores": scores,
        "coverage": coverage,
        "honesty": honesty,
    }
