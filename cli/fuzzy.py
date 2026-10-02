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

from typing import (
    Any,
    Callable,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

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


# ---------------------------------------------------------------------------
# PHRASE matching (VEX-PF-06)
#
# `fuzzy_score` above is the RIGHT matcher for a palette entry and the WRONG
# one for a question, and the measurement is why: it ANDs each query WORD
# against one long candidate string, so a four-word question is satisfied by a
# 200-character summary as long as each word appears somewhere in it, in order.
# "show me what changed" therefore matched `/help`'s summary, `/cost`'s
# summary and `/steer`'s summary, and the ranking between them was decided by
# position and length. `phrase_score` scores the query as a WHOLE PHRASE
# against a whole phrase, which is the shape the input actually has.
#
# The tiers are deliberately far apart, because the failure being removed is
# "the right answer was third", not "the right answer was missing":
#
#   exact phrase                     100000
#   query is a contiguous sub-phrase   40000
#   query is an ordered sub-sequence  20000
#   whole-word coverage only            900 * fraction
# ---------------------------------------------------------------------------

_PHRASE_EXACT = 100_000
_PHRASE_CONTIGUOUS = 40_000
_PHRASE_SEQUENCE = 20_000
_PHRASE_PER_WORD = 300

#: Tokens a PHRASE query drops before matching, because "show me what changed"
#: is three signal words wearing a two-word coat. This is the same reasoning as
#: `cli.interactive._HELP_STOPWORDS`, kept here so the primitive is usable
#: without importing the interactive layer - and deliberately SMALLER: a phrase
#: matcher that drops "what" cannot rank "what did it cost" at all.
_PHRASE_NOISE = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "can",
        "did",
        "do",
        "does",
        "for",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "show",
        "that",
        "the",
        "to",
        "was",
        "were",
        "what",
        "when",
        "which",
        "with",
        "you",
        "your",
    }
)


def normalize_phrase(text: Any) -> Tuple[str, ...]:
    """Lowercase ALPHANUMERIC word tokens for PHRASE matching.

    Every other character - punctuation, brackets, slashes, apostrophes,
    hyphens - becomes a space. Splitting the hyphen is deliberate: "undo-that"
    and "undo that" are the same question to a person, and keeping the hyphen
    made them score as two different queries. Splitting the apostrophe is the
    same argument for "don't".

    Total and never raises: a non-string is `str()`'d, which is the module's
    standing contract. A bracket survives nowhere in the output, which is one
    reason the onboarding surface can hand these tokens to a renderer.
    """
    raw = str(text or "")
    table = {ord(c): (c if c.isalnum() else " ") for c in raw}
    return tuple(token for token in raw.casefold().translate(table).split() if token)


def signal_phrase(text: Any) -> Tuple[str, ...]:
    """`normalize_phrase` with the noise words dropped.

    Falls back to the full token list when dropping would leave nothing, so
    "show" and "the" are searchable in their own right and a query made only
    of noise words never returns an empty token list (which would make every
    candidate score identically and the ranking arbitrary).
    """
    tokens = normalize_phrase(text)
    kept = tuple(token for token in tokens if token not in _PHRASE_NOISE)
    return kept or tokens


def _contiguous_span(needle: Tuple[str, ...], hay: Tuple[str, ...]) -> bool:
    """True when `needle` appears in `hay` as one unbroken run of words."""
    if not needle or len(needle) > len(hay):
        return False
    first = needle[0]
    for index, word in enumerate(hay):
        if word == first and hay[index : index + len(needle)] == needle:
            return True
    return False


def _ordered_subsequence(needle: Tuple[str, ...], hay: Tuple[str, ...]) -> bool:
    """True when every needle word appears in `hay` in the same order.

    Gaps are allowed; order is not. "changed show" is NOT a subsequence of
    "show me what changed", which is correct - a person who typed the words in
    the wrong order has not asked that question.
    """
    cursor = 0
    for word in needle:
        while cursor < len(hay) and hay[cursor] != word:
            cursor += 1
        if cursor >= len(hay):
            return False
        cursor += 1
    return True


def phrase_score(query: str, phrase: str) -> Optional[int]:
    """Score a whole free-text QUERY against a whole multi-word PHRASE.

    Returns a higher-is-better int, or None when the query is not a match at
    all. An empty or whitespace query returns 0 - "matches everything, no
    preference" - so a caller can render an unfiltered list through the same
    call it uses to rank a filtered one. Case-insensitive, punctuation-blind,
    and never raises.
    The scoring is four tiers, far apart on purpose (see the constants above):
    an exact phrase beats a contiguous sub-phrase, which beats an ordered
    sub-sequence, which beats whole-word coverage. Coverage alone is not a
    match on a single common word: `stop` in "stop the run" must not be able to
    out-rank the row that answers the whole question, and a one-word query
    against a long phrase cannot.
    """
    q_raw = normalize_phrase(query)
    p_raw = normalize_phrase(phrase)
    if not q_raw:
        # An empty or whitespace query matches everything with no preference,
        # so a caller can render an unfiltered list through the same call.
        return 0
    if not p_raw:
        return None
    if q_raw == p_raw:
        # Compared on the RAW normalisation, before noise words are dropped.
        # Compared on the signal tokens instead, "undo that" reduces to
        # ("undo",) and can never equal ANY phrase, so the exact tier — the
        # strongest answer this function can give — was unreachable. Measured:
        # "undo that" against "undo that" scored 40010, the same as against
        # "undo that and the redo step".
        return _PHRASE_EXACT
    q_tokens = signal_phrase(query)
    p_tokens = p_raw
    if _contiguous_span(q_tokens, p_tokens):
        return _PHRASE_CONTIGUOUS + len(q_tokens) * 10
    if _ordered_subsequence(q_tokens, p_tokens):
        return _PHRASE_SEQUENCE + len(q_tokens) * 10
    p_set = set(p_tokens)
    hits = sum(1 for token in q_tokens if token in p_set)
    if not hits:
        return None
    # Coverage is deliberately the weakest tier and is penalised by fraction,
    # so a query that matches one word of five cannot outrank a query that
    # matched three of three.
    return int(_PHRASE_PER_WORD * hits / len(q_tokens)) * 10


# ---------------------------------------------------------------------------
# MULTI-FIELD scoring (VEX-CS-03)
#
# `fuzzy_score` above is right for ONE candidate string, and `phrase_score` is
# right for one whole question. Neither is right for a MENU, where a row is four
# different strings of very different kinds: a name ("/undo"), a one-line
# description, a group heading ("put it back"), and a corpus of phrasings a
# person would actually type.
#
# Concatenating them is the obvious move and it is wrong for a measurable
# reason: a 40-character group heading plus a 60-character description turns
# every three-word query into a match against a 100-character haystack, so the
# ranking between the two rows that match is decided by string length. These
# helpers score each field SEPARATELY and combine with declared weights, so a
# name hit and a description hit are comparable numbers rather than an accident
# of concatenation.
# ---------------------------------------------------------------------------

#: Per-field weights, best-first by intent. A name is what the user typed; a
#: group heading is a coarse intent ("I want to stop it"); a description is the
#: last resort.
#:
#: These multiply a *TIER* (0-100, see `_fuzzy_tier`), never a raw
#: `fuzzy_score`. The first version of this multiplied the raw score and it was
#: wrong in a way worth recording: `fuzzy_score` returns ~1000 for a substring
#: hit but can reach several thousand for a scattered subsequence over a long
#: description, so `/detach`'s summary "leave the run running and stop
#: watching it" out-scored `/cancel`'s corpus hit for the query "stop the run".
#: A tier is bounded, so a weight is a real priority rather than a number that
#: happens to be larger.
FIELD_WEIGHTS: dict = {
    "name": 100,
    "alias": 90,
    "phrasing": 80,
    "argument": 40,
    "description": 30,
    "group": 20,
    "kind": 10,
}

#: The tier a `phrase_score` result maps to. `phrase_score` is already tiered
#: (100000 exact / 40000 contiguous / 20000 ordered-subsequence / ~900 whole-
#: word coverage); this translates it onto the same 0-100 scale as
#: `_fuzzy_tier` so the two matchers are comparable, and keeps the ordering
#: inside the coverage tier by fraction.
PHRASE_TIER_EXACT = 100
PHRASE_TIER_CONTIGUOUS = 90
PHRASE_TIER_SEQUENCE = 70
PHRASE_TIER_COVERAGE_MAX = 20


def _fuzzy_tier(query: str, candidate: str) -> Optional[int]:
    """Map one `fuzzy_score` result onto a bounded 0-100 tier.

    Tiers rather than the raw score, because the raw score is unbounded above
    and grows with the LENGTH of the candidate: a scattered subsequence over a
    60-character summary scores several times what a substring hit on a
    5-character name does, which is exactly backwards. The tiers are:

    * 100 - the query is a substring of the candidate (a contiguous run)
    * 80  - every query character matches at a word boundary
    * 50  - a scattered subsequence matches
    * 25  - matched, but only after normalisation made it possible

    None means "this field does not match at all", which is different from
    every one of these. Never raises.
    """
    if not candidate:
        return None
    score = fuzzy_score(query, candidate)
    if score is None:
        return None
    needle = "".join(str(query or "").lower().split())
    hay = str(candidate).lower()
    if needle and needle in hay:
        return 100
    if score >= 1000:
        # A high raw score without a substring means boundary/camel hits.
        return 80
    if score > 0:
        return 50
    return 25


def _phrase_tier(query: str, phrase: str) -> Optional[int]:
    """Map one `phrase_score` result onto the same 0-100 scale as `_fuzzy_tier`.

    The tier boundaries are `phrase_score`'s own, read from its constants
    rather than restated as bare numbers, so a future retune of the phrase
    scale cannot silently leave this table behind. Within the coverage tier the
    fraction is preserved, because "matched two words of two" and "matched one
    word of two" are different answers. None when the phrase does not match.
    """
    if not phrase:
        return None
    score = phrase_score(query, phrase)
    if score is None:
        return None
    if score >= _PHRASE_EXACT:
        return PHRASE_TIER_EXACT
    if score >= _PHRASE_CONTIGUOUS:
        return PHRASE_TIER_CONTIGUOUS
    if score >= _PHRASE_SEQUENCE:
        return PHRASE_TIER_SEQUENCE
    # Coverage tier: `phrase_score` returns int(PER_WORD * fraction) * 10.
    top = _PHRASE_PER_WORD * 10
    if top <= 0:
        return PHRASE_TIER_COVERAGE_MAX
    fraction = max(0.0, min(1.0, float(score) / float(top)))
    return max(1, int(PHRASE_TIER_COVERAGE_MAX * fraction))


def field_score(query: str, fields: "Mapping[str, Any] | None") -> Optional[int]:
    """Best weighted TIER across several named fields of one row.

    Each field is scored on its OWN string, so a hit in a short field is
    comparable with a hit in a long one. The best field wins; the rest are
    ignored rather than summed, because summing means a row with four weak
    matches outranks a row with one exact match, and an exact match is what
    the user typed. `FIELD_WEIGHTS` declares the order, and it includes
    `phrasing` - a whole sentence scored by `phrase_score`, on the same scale.

    A `phrasing` field may be a single string OR a sequence of strings, in
    which case each is scored and the best wins. That matters: a corpus is a
    LIST of phrasings, and joining them into one haystack destroys the exact
    tier, because "stop the run" stops being a contiguous run of the joined
    string. Scored per phrasing, it is an exact match.

    Returns None when no field matches at all, which is how a caller
    distinguishes "not a match" from "matched weakly". Never raises; a
    non-mapping is treated as empty and a hostile field type is skipped.
    """
    if not fields:
        return None
    best: Optional[int] = None
    for name, weight in FIELD_WEIGHTS.items():
        raw = fields.get(name)
        if not raw:
            continue
        candidates: List[str]
        if isinstance(raw, str):
            candidates = [raw]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            candidates = [str(item) for item in raw if str(item or "").strip()]
        else:
            continue
        for candidate in candidates:
            try:
                tier = (
                    _phrase_tier(query, candidate)
                    if name == "phrasing"
                    else _fuzzy_tier(query, candidate)
                )
            except Exception:
                continue
            if tier is None:
                continue
            weighted = int(tier * weight)
            if best is None or weighted > best:
                best = weighted
    return best


def entry_score(
    query: str,
    *,
    name: str = "",
    alias: str = "",
    argument: str = "",
    description: str = "",
    group: str = "",
    kind: str = "",
    phrasing: Any = "",
) -> Optional[int]:
    """Score a menu row from its name/description/group AND a task phrasing.

    A thin, honest wrapper over `field_score`, kept as a NAMED function
    because it is the vocabulary a menu row is described in, and a caller
    that wants a row score should not have to know the field weight table. The
    scaling problem it originally solved is now solved inside `field_score` by
    the tier mapping, so this no longer mixes two incompatible scales.

    An empty query scores 0, which is "matches everything, no preference", so
    a caller can render an unfiltered menu through the same call it uses to
    rank a filtered one. None when nothing matches, so a caller can FILTER
    rather than rank a row that does not match at all. Never raises.
    """
    if not str(query or "").strip():
        return 0
    return field_score(
        query,
        {
            "name": name,
            "alias": alias,
            "argument": argument,
            "description": description,
            "group": group,
            "kind": kind,
            "phrasing": phrasing,
        },
    )


def rank_entries(
    records: Sequence[Any],
    query: str,
    fields: Callable[[Any], Mapping[str, str]],
    *,
    limit: Optional[int] = None,
) -> List[Any]:
    """Rank arbitrary records by `entry_score` against their named fields.

    The multi-field sibling of `filter_and_rank`: `fields(record)` returns the
    row's name/alias/argument/description/group/kind/phrasing, and this applies
    `entry_score` to each. Original order is preserved on ties, which is what
    keeps a curated menu in its curated order when the user has not typed
    anything. Records whose `fields` raises are SKIPPED rather than fatal: a
    menu that dies because one row's description is an object is a menu that
    cannot show the other fifty rows. Never raises.
    """
    needle = str(query or "")
    if not needle.strip():
        return (
            list(records)[:limit] if limit is not None and limit >= 0 else list(records)
        )
    scored: List[Tuple[Any, int, int]] = []
    for index, rec in enumerate(records):
        try:
            data = fields(rec)
        except Exception:
            continue
        try:
            score = entry_score(needle, **(data or {}))
        except TypeError:
            # A `fields` callable that returned something entry_score cannot
            # consume is a caller bug, not a reason to lose the whole menu.
            try:
                score = field_score(needle, data if isinstance(data, dict) else None)
            except Exception:
                continue
        except Exception:
            continue
        if score is not None:
            scored.append((rec, score, index))
    scored.sort(key=lambda t: (-t[1], t[2]))
    out: List[Any] = [rec for rec, _s, _i in scored]
    if limit is not None and limit >= 0:
        return out[:limit]
    return out


def phrase_rank(
    records: Sequence[Any],
    query: str,
    key: Callable[[Any], str],
    *,
    phrase_key: Optional[Callable[[Any], str]] = None,
    limit: Optional[int] = None,
) -> List[Any]:
    """Rank arbitrary records by `phrase_score` against `key(record)`.

    `phrase_key` is an optional WIDER haystack (a row's phrase plus its
    synonyms, say) consulted when the primary key misses, scored below the
    primary so a label always beats a synonym. Original order is preserved on
    ties, which is what keeps a curated catalogue in its curated order. Never
    raises; records whose key raises are skipped, not fatal.
    """
    scored: List[Tuple[Any, int, int]] = []
    for index, rec in enumerate(records):
        try:
            primary = str(key(rec) or "")
        except Exception:
            continue
        score = phrase_score(query, primary)
        if score is None and phrase_key is not None:
            try:
                score = phrase_score(query, str(phrase_key(rec) or ""))
                if score is not None:
                    score -= 1  # a synonym match never outranks the label
            except Exception:
                score = None
        if score is not None:
            scored.append((rec, score, index))
    scored.sort(key=lambda t: (-t[1], t[2]))
    out = [rec for rec, _s, _i in scored]
    if limit is not None and limit >= 0:
        return out[:limit]
    return out
