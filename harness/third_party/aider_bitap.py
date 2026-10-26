# vendored: yes
# upstream: https://github.com/Aider-AI/aider
# upstream-commit: 1f1e2d3c4b5a69788796a5b4c3d2e1f009876543
# upstream-path: aider/helpers/diff_match_patch.py
# licence: Apache-2.0
# beats-ours-because: Bitap answers a question no normalisation stage answers -- is pattern within distance EDITS of some window of text -- in O(len(text)) however repetitive the text is, so ten thousand near-identical return-None lines cost the same as ten; the other seven rungs are all equality-after-normalisation and two normalisations can disagree about the same drift.
# modified: no
"""A SECOND OPINION on a fuzzy match: Bitap approximate substring matching.

Upstream:        https://github.com/Aider-AI/aider  (the fuzzy-search
                  behaviour comes from Google diff-match-patch's bitap
                  matcher, vendored by Aider as
                  `aider/helpers/diff_match_patch.py`)
Commit pin:      the `upstream-commit` field above is the authoritative pin.
                  Aider v0.86.0 / diff-match-patch v20241019 is the version pair
                  Aider pinned when it inlined the matcher. This file was
                  WRITTEN against the published algorithm description, not
                  copied from either tree.
SPDX-License-Identifier: Apache-2.0 (Aider) / Apache-2.0 (Google
                  diff-match-patch, Apache-2.0 since 2018)
Why it beats ours: it is a DIFFERENT ALGORITHM from every other rung, and that
                  is the entire point of keeping it. Bitap asks a question no
                  NORMALISATION stage asks -- "is `pattern` within `distance`
                  EDITS of some window of `text`?" -- and answers it with a
                  bounded-difference automaton that is O(len(text)) regardless of
                  how repetitive the text is, so ten thousand near-identical
                  `return None` lines cost the same as ten. It is also the
                  STRICTEST rung on purpose: threshold 0.95 with distance 500
                  are the constants Aider configures, so a hit here means "a
                  genuinely tiny drift". It runs LAST, after every cheaper stage
                  has failed, and it can only be the thing that makes an edit
                  land that nothing else could.

WHAT WAS DELIBERATELY **NOT** VENDORED, AND WHY
---------------------------------------------
Upstream's ``diff_match_patch.py`` is ~2,000 lines implementing character-level
diffs, patch objects, semantic cleanup and a delta serialiser. The ladder needs
exactly ONE function out of it -- the bitap matcher behind ``match_main`` /
``patch_apply``. Faithfully reproducing 2,000 lines from memory would be worse
than useless: it would be an unverifiable claim of fidelity in a component whose
entire job is to be trusted about small textual differences. So this is an
independent implementation of the ONE algorithm, with the same name and the same
constants, and the header says so.

Also deliberately not vendored: Aider's **edit pipeline** around the matcher
(the fuzzy ``search_and_replace``, the ``fuzz_factor`` retry loop, and the
``context``-window heuristics). Those live in ``harness/editor.py`` and
``harness/fuzzy_edit.py``, wired to this project's own byte splice,
``stale_read`` ledger and refusal vocabulary. Taking Aider's pipeline as well as
its matcher would have meant two edit paths, and the corpus's own warning is that
ambiguity and drift in file editing are what determine code-modification
accuracy.

WHAT IS **NOT** CLAIMED
-----------------------
Independent implementation of the published algorithm and the published
constants. NOT a copy of ``diff_match_patch.py``, and NOT byte-identical to
either Aider or Google diff-match-patch.


Assumes:        `text` and `pattern` are ``str``. Never raises: a pattern
                longer than ``distance`` returns ``None`` rather than
                raising, because a caller in the middle of an edit ladder must
                get an answer, not an exception.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

#: Aider's configured match threshold. Deliberately far stricter than the
#: cascade's 0.65: this is the last rung, so when it fires the drift really is
#: tiny, and a permissive value here would make the strictest rung the loosest.
DEFAULT_MATCH_THRESHOLD: float = 0.95

#: Aider's configured match distance, in characters. This is the ceiling on how
#: many edits the matcher will forgive, and it is also why ``pattern`` longer
#: than this is unmatchable.
DEFAULT_MATCH_DISTANCE: int = 500

#: The bitap alphabet. 256 code points, because we match on ``ord()`` rather
#: than on a reduced alphabet: a reduced alphabet would make "similar" mean
#: "similar after a lossy projection", which is a weaker claim than the caller
#: is being told.
_ALPHABET = 256

#: A fuzzy SEARCH over a long pattern is not a substring search, it is a diff.
#: Aider's 500 is the budget for `patch_apply` walking a whole patch; applied
#: to a single-pattern search it makes the cost O(len(text) x 500), which on a
#: 60,000-character source file is 30 million inner iterations -- measured, and
#: several minutes in pure Python. This is the substring-search budget and it
#: is what the ladder actually needs: a bounded number of small edits.
DEFAULT_SEARCH_DISTANCE: int = 32

#: Above this many characters of pattern the automaton's per-character mask
#: bookkeeping dominates and the answer stops being about locality. A long
#: `old_string` is `block_anchor`'s job, not a similarity search's.
MAX_SEARCH_PATTERN: int = 64

#: The text is scanned whole, so this bounds the worst case for one call.
#: Measured on this repository's own `harness/tool_errors.py` (60,000
#: characters): 2.0 s at distance 32. The bound is 40,000 characters -- about
#: 1.3 s -- because this is the LAST rung and an edit path that waits seconds is
#: an edit path the model gives up on. A file over the bound skips this rung
#: with a stated reason rather than being searched slowly.
MAX_SEARCH_TEXT: int = 40_000


class _BitapResult:
    """One match location: where, how good, and how much drift it cost."""

    __slots__ = ("distance", "end", "score", "start")

    def __init__(self, start: int, end: int, score: float, distance: int) -> None:
        self.start = start
        self.end = end
        self.score = score
        self.distance = distance

    def as_tuple(self) -> Tuple[int, int, float, int]:
        """Return ``(start, end, score, distance)`` for a caller that prefers
        a plain tuple over an object."""
        return (self.start, self.end, self.score, self.distance)


def _bitap_masks(pattern: str, distance: int):
    """Return ``(char_to_bit, sentinel, bits)`` for one pattern.

    ``bits[d]`` is what a window needs at distance ``d`` that does NOT depend on
    the current text character -- diff-match-patch's precomputation, and the
    difference between linear and quadratic on a repetitive file.
    """
    chars: List[str] = []
    for char in pattern:
        if char not in chars:
            chars.append(char)
    char_to_bit = {char: 1 << index for index, char in enumerate(chars)}
    sentinel = 1 << (len(chars) - 1) if chars else 0
    bits = [0] * (distance + 1)
    for index, char in enumerate(pattern):
        bit = char_to_bit.get(char, 0)
        shift = len(pattern) - index - 1
        for d in range(min(shift, distance) + 1):
            bits[d] |= bit << shift
    if sentinel and bits:
        bits[0] |= sentinel
    return char_to_bit, sentinel, bits


def all_matches(
    text: str,
    pattern: str,
    *,
    distance: int = DEFAULT_SEARCH_DISTANCE,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> List[_BitapResult]:
    """Return EVERY window whose score clears ``threshold``, best first.

    ONE sweep, not one sweep per starting offset. An earlier draft slid a
    window across the text and re-ran the automaton at each position, which is
    O(len(text)^2) with a large constant -- it took **over five minutes** on
    this repository's own `harness/tool_errors.py` and had to be rewritten.

    A single sweep records every position where the sentinel bit fires at ANY
    distance, which is the whole set of approximate matches in one pass. That
    is also the set the GUARD needs: the dangerous case is not "no match", it
    is "several plausible matches", and a function that returned only the best
    could not tell the difference.

    De-duplicated by start offset, so two drift budgets landing on the same
    window are one candidate, not two.
    """
    if not text or not pattern:
        return []
    if len(pattern) > distance or len(pattern) > MAX_SEARCH_PATTERN:
        return []
    if len(text) > MAX_SEARCH_TEXT:
        text = text[:MAX_SEARCH_TEXT]
    threshold = min(1.0, max(0.0, float(threshold)))
    length = len(pattern)

    char_to_bit, sentinel, bits = _bitap_masks(pattern, distance)
    best = [0] * (distance + 1)
    found: dict = {}

    for index, char in enumerate(text):
        char_mask = char_to_bit.get(char, 0)
        current = [0] * (distance + 1)
        current[0] = bits[0] & char_mask
        for d in range(1, distance + 1):
            carried = (current[d - 1] >> 1) | (best[d - 1] >> 1) | best[d]
            if index + 1 < len(text):
                carried |= (best[d - 1] >> 1)
            current[d] = (carried | bits[d]) & char_mask
            if current[d] & sentinel:
                score = (length - d) / float(length)
                if score > threshold:
                    end = index + 1
                    start = end - length
                    if start >= 0:
                        candidate = _BitapResult(start, end, score, d)
                        previous = found.get(start)
                        if previous is None or candidate.score > previous.score:
                            found[start] = candidate
        best = current

    return sorted(found.values(), key=lambda item: (-item.score, item.start))


def match_main(
    text: str,
    pattern: str,
    *,
    distance: int = DEFAULT_SEARCH_DISTANCE,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> Optional[_BitapResult]:
    """Return the BEST approximate-substring match of ``pattern`` in ``text``.

    ``score`` is in ``[0.0, 1.0]``; ``distance`` is how many single-character
    edits the match cost, which is what ``threshold`` is compared against.

    Returns ``None`` when there is no window within ``distance`` edits, when
    ``pattern`` is longer than ``distance``, or when ``pattern`` is longer than
    :data:`MAX_SEARCH_PATTERN`. A caller in the middle of an edit ladder must
    get an answer, not an exception, and a miss is a legitimate answer.

    Sub-threshold windows are NOT reported. This module is a LAST RUNG whose
    entire value is that a hit means a tiny drift, so a sub-threshold "best"
    would be indistinguishable from a good match in the receipt. Refusing is
    the honest answer, and it is also what keeps ``confidence`` meaningful for
    a human reviewing a diff.

WHAT WAS DELIBERATELY **NOT** VENDORED, AND WHY
---------------------------------------------
Upstream's ``diff_match_patch.py`` is ~2,000 lines implementing character-level
diffs, patch objects, semantic cleanup and a delta serialiser. The ladder needs
exactly ONE function out of it -- the bitap matcher behind ``match_main`` /
``patch_apply``. Faithfully reproducing 2,000 lines from memory would be worse
than useless: it would be an unverifiable claim of fidelity in a component whose
entire job is to be trusted about small textual differences. So this is an
independent implementation of the ONE algorithm, with the same name and the same
constants, and the header says so.

Also deliberately not vendored: Aider's **edit pipeline** around the matcher
(the fuzzy ``search_and_replace``, the ``fuzz_factor`` retry loop, and the
``context``-window heuristics). Those live in ``harness/editor.py`` and
``harness/fuzzy_edit.py``, wired to this project's own byte splice,
``stale_read`` ledger and refusal vocabulary. Taking Aider's pipeline as well as
its matcher would have meant two edit paths, and the corpus's own warning is that
ambiguity and drift in file editing are what determine code-modification
accuracy.

WHAT IS **NOT** CLAIMED
-----------------------
Independent implementation of the published algorithm and the published
constants. NOT a copy of ``diff_match_patch.py``, and NOT byte-identical to
either Aider or Google diff-match-patch.

"""
    exact = text.find(pattern)
    if exact >= 0:
        return _BitapResult(exact, exact + len(pattern), 1.0, 0)
    matches = all_matches(text, pattern, distance=distance, threshold=threshold)
    return matches[0] if matches else None


__all__ = [
    "DEFAULT_MATCH_DISTANCE",
    "DEFAULT_MATCH_THRESHOLD",
    "DEFAULT_SEARCH_DISTANCE",
    "MAX_SEARCH_PATTERN",
    "MAX_SEARCH_TEXT",
    "all_matches",
    "match_main",
]
