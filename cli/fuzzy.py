"""Fuzzy subsequence matching for the TUI command palette (Task A).

A tiny, dependency-free matcher modelled on the feel of VS Code's /
fzf's command palette: a query matches a candidate when every query
character appears in order (case-insensitively), and the match is
SCORED so better matches sort first — prefix hits, word-boundary hits
(path/identifier segments), and contiguous runs rank above scattered
ones. Pure and total: every function accepts any string and never
raises, so a palette can feed it raw user typing safely.

The module is UI-agnostic on purpose (no textual/rich imports): the
palette, tests, and any future completion surface share one matcher.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

#: Characters that start a new "word" for boundary bonuses. Matching a
#: query character at one of these is much more likely to be what the
#: user meant (palette labels are full of paths and dotted names).
_BOUNDARY = set(" /\\\\._-:>|@+")

_SCORE_START = 16  # candidate begins exactly with a query char
_SCORE_BOUNDARY = 10  # char follows a separator
_SCORE_CAMEL = 7  # camelCase hump (prev lower, this upper)
_SCORE_CONSECUTIVE = 6  # immediately follows the previous match
_SCORE_BASE = 1  # any other match
_PENALTY_GAP = 1  # per skipped character between matches
_PENALTY_TRAILING = 0  # trailing unmatched chars do not matter


def _word_score(word: str, c: str) -> Optional[int]:
    """Score ONE lowercased query word against a lowercased candidate.
    Substring fast-path first, then a gap/bonus-weighted subsequence.
    None when the word is not a subsequence."""
    pos = c.find(word)
    if pos >= 0:
        bonus = (
            _SCORE_START
            if pos == 0
            else (_SCORE_BOUNDARY if c[pos - 1] in _BOUNDARY else 0)
        )
        return 1000 + bonus * 2 - pos - (len(c) - pos - len(word)) // 4
    score = 0
    last = -1
    prev_lower = False
    for ch in word:
        idx = c.find(ch, last + 1)
        if idx < 0:
            return None
        if idx == 0:
            score += _SCORE_START
        elif c[idx - 1] in _BOUNDARY:
            score += _SCORE_BOUNDARY
        elif prev_lower and ch.isalpha() and c[idx].isupper():
            score += _SCORE_CAMEL
        elif idx == last + 1:
            score += _SCORE_CONSECUTIVE
        else:
            score += _SCORE_BASE
        if last >= 0:
            score -= min((idx - last - 1) * _PENALTY_GAP, 8)
        score -= _PENALTY_TRAILING
        prev_lower = c[idx].islower() if idx < len(c) else False
        last = idx
    return score


def fuzzy_score(query: str, candidate: str) -> Optional[int]:
    """Score how well `query` fuzzy-matches `candidate`.

    Returns an int score (higher is a better match) or None when the
    query is NOT a match. Whitespace separates query WORDS and every
    word must match (AND) — "cli trce" means "cli ... and a trce
    subsequence", never a literal "cli␣trce" run — so a space is the
    natural filter separator a user expects. An empty/whitespace query
    matches everything with score 0 (the palette shows the full list
    before the user types). Case-insensitive. Never raises.

    Assumes query and candidate are strings (non-strings are str()'d —
    the module is total by contract); the score ordering is stable only
    within a single call site (absolute values are opaque).
    """
    c = str(candidate or "").lower()
    words = [w for w in str(query or "").lower().split() if w]
    if not words:
        return 0
    if not c:
        return None
    total = 0
    for w in words:
        s = _word_score(w, c)
        if s is None:
            return None
        total += s
    # A shorter candidate that consumed the same words is a tighter fit.
    total -= len(c) // 12
    return total


def rank(items: Iterable[str], query: str) -> List[Tuple[str, int]]:
    """(item, score) pairs for items matching `query`, best first.

    Stable for ties (original order preserved), which keeps the
    built-in command list's curated order when no query is typed.
    Never raises; items are str()'d for matching. Deliberately
    string-only — callers rank strings and map back with their own keys.
    """
    out: List[Tuple[str, int]] = []
    for item in items:
        s = fuzzy_score(query, item)
        if s is not None:
            out.append((str(item), s))
    out.sort(key=lambda pair: -pair[1])
    return out


def filter_and_rank(
    records: Sequence[Any],
    query: str,
    key: Callable[[Any], str],
    limit: Optional[int] = None,
) -> List[Any]:
    """Filter arbitrary records by fuzzy-matching `key(record)`.

    A record matches on its primary key; when the key misses, a hit on
    the record's ``hint`` (attribute OR mapping key, whichever holds it)
    still counts with a reduced score — so typing "resumable" finds a
    session whose hint says so even though its label is `/resume fix-abc`.
    Best-first, original order preserved on ties, capped at `limit` when
    given. Never raises.
    """
    scored: List[Tuple[Any, int, int]] = []
    for i, rec in enumerate(records):
        primary = key(rec)
        s = fuzzy_score(query, primary)
        if s is None:
            hint = ""
            try:
                if isinstance(rec, dict):
                    hint = str(rec.get("hint") or "")
                else:
                    hint = str(getattr(rec, "hint", "") or "")
            except Exception:
                hint = ""
            if hint:
                hs = fuzzy_score(query, hint)
                if hs is not None:
                    s = hs - 500  # a hint match ranks below label matches
        if s is not None:
            scored.append((rec, s, i))
    scored.sort(key=lambda t: (-t[1], t[2]))
    out = [rec for rec, _s, _i in scored]
    if limit is not None and limit >= 0:
        return out[:limit]
    return out
