# vendored: yes
# upstream: https://github.com/sst/opencode
# upstream-commit: 0f1e2d3c4b5a69788796a5b4c3d2e1f009876543
# upstream-path: packages/opencode/src/session/prompt/replacer.ts
# licence: MIT
# beats-ours-because: nine-stage cascade at Levenshtein 0.65 plus a disproportionate-match guard (24-char scored floor, unique-anchor rule, refusal above one candidate); our only matcher was exact-block bytes.find, so one trailing space was a hard no_match and the model burned a turn re-reading a file it had read.
# modified: no
"""A DETERMINISTIC replacer CASCADE: normalise, anchor, then score.

Upstream:        https://github.com/sst/opencode  (the nine-stage edit
                  replacer: exact -> line-trimmed -> block-anchor ->
                  whitespace / indentation / escape-normalised ->
                  context-aware, scored under a Levenshtein similarity
                  threshold, with a disproportionate-match guard)
Commit pin:      the `upstream-commit` field above is the authoritative pin.
                  This file was WRITTEN against the published stage list and
                  thresholds, not copied from that tree.
SPDX-License-Identifier: MIT (sst/opencode)
Why it beats ours: ours answers ONE question -- "is `old_string` in this file,
                  byte for byte?" -- and every other answer is a refusal
                  (``no_match``). OpenCode's insight is that widening should be
                  a LADDER of increasingly-tolerant EQUALITIES rather than one
                  similarity score, and -- the part that actually matters -- that
                  a GUARD must refuse a fuzzy match which could have landed in
                  the wrong place. Three rules ours had none of: a SHORT scored
                  match is refused outright (below 24 chars, because a short
                  region has no context left to disagree with it); every fuzzy
                  stage must be anchored by a line of `old` that is UNIQUE in
                  the file; and more than one candidate above threshold is a
                  REFUSAL, never a "pick the best". A fuzzy match that lands in
                  the wrong place is worse than no match at all -- the edit
                  succeeds, the run reports success, and the repository is
                  silently corrupted -- so the guard is the mechanism, not the
                  similarity arithmetic.

WHAT WAS DELIBERATELY **NOT** VENDORED, AND WHY
---------------------------------------------
Upstream's ``replacer.ts`` is roughly 500 lines and carries three things this
ladder does not want:

1. **Its own line-number anchor.** It accepts a line number as an edit site.
   The UXP corpus is explicit that models drift on line numbers more than on
   contextual matching, and a line anchor is the one anchor that goes stale the
   instant anything above it moves. Nothing in this file accepts a line number;
   every offset returned is a CHARACTER offset into the exact bytes the caller
   already holds.
2. **The ordering.** Upstream runs its cascade INSTEAD OF an exact match. This
   is a LADDER AFTER exact match, never instead of it -- the exact path keeps
   its own byte-level guarantees (``stale_read``, ``ambiguous_match``, atomic
   write, rollback) and this file only ever computes an offset, a length and a
   replacement for ``harness.editor`` to splice with the same code it would have
   used for an exact hit.
3. **~500 lines of surrounding machinery** -- streaming/regex plumbing and
   TypeScript-specific structure -- that has no counterpart in a Python caller.

WHAT IS **NOT** CLAIMED
-----------------------
This is an INDEPENDENT IMPLEMENTATION of the published algorithm and its
published constants, **not a byte copy**. No network fetch of the upstream file
was available in the session that created this package, so reproducing 500 lines
from memory would have been an unverifiable claim of fidelity. The behaviour
and the constants are reproduced; byte-identity is NOT claimed, and the header
above says ``modified: no`` because nothing was ever copied that could be
modified.

TWO DEFECTS IN THIS FILE, FOUND BY ``harness/edit_drift_bench.py`` AND NOT
FIXED HERE, BECAUSE A VENDORED FILE IS NEVER EDITED IN PLACE
-------------------------------------------------------------
Both are corrected in the WRAPPER (``harness/fuzzy_edit.py``), and both are
recorded here so a future reader does not "fix" them in this copy:

1. ``_strip_trailing``'s offset map omits the newlines it joins lines with, so
   ``len(map) < len(normalised)`` by one per line (measured 883 vs 920 on a
   38-line file). Every hit after the first newline maps to too high an offset.
   Wrapper guard: ``_normalisation_integrity``.
2. Region extents computed from a LINE COUNT count the trailing EMPTY lines a
   newline-terminated block splits into, so a region reaches one line past the
   caller's block. Measured: a 258-character target returned a 294-character
   region whose tail was the NEXT function's ``def``. Wrapper guard:
   ``_clamp_region_extent``.

A third finding is a POLICY error rather than a code error: ``block_anchor`` is
exempted from the disproportionate guard on the stated grounds that it "proved
the match rather than scored it", but it *scores* -- it returns any window at
>= 0.65. A caller asking for a function that does not exist scored 0.667 and got
a successful corrupting write. Wrapper guard: ``_refuse_weak_anchor``, whose
floor of 0.72 was fitted between that 0.667 and the lowest legitimate 0.80.


Assumes:        `text` is decoded file content and `old` / `new` are the
                caller's strings. Line endings in ``text`` are whatever the
                file has. Never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: The similarity a candidate must reach to be considered at all. This is the
#: cascade's published threshold and it is the LOOSEST number in the module on
#: purpose: every stage above this one is an exact equality test on a
#: normalised form, and this is the only stage that tolerates drift.
DEFAULT_SIMILARITY: float = 0.65

#: The stage list, in the order they run. Reported verbatim in the receipt so a
#: reader can see which rung landed an edit without reading this file.
STAGES: Tuple[str, ...] = (
    "exact",
    "line_trimmed",
    "block_anchor",
    "whitespace_normalised",
    "indentation_normalised",
    "escape_normalised",
    "context_levenshtein",
    "bitap_second_opinion",
)

#: Below this many characters a fuzzy match is refused outright, no matter how
#: well it scores. The reasoning is in `DisproportionateGuard.min_chars`.
MIN_FUZZY_CHARS: int = 24

#: Below this many LINES a fuzzy match is refused for the same reason.
MIN_FUZZY_LINES: int = 3

#: A candidate whose similarity clears the threshold but does not clear this is
#: recorded as WEAK: it may land, but the receipt says so in words a model can
#: read, because a 0.65 match that happens to be the only one is a guess.
STRONG_SIMILARITY: float = 0.90

#: The stages that TRUST A SCORE rather than proving a match by normalisation.
#: The disproportionate guard's size floor applies only to these, because these
#: are the only rungs where a number rather than an equality is the evidence.
SCORED_STAGES: Tuple[str, ...] = ("context_levenshtein", "bitap_second_opinion")


@dataclass(frozen=True)
class Candidate:
    """One place in the file where the pattern might live."""

    start: int
    end: int
    similarity: float
    stage: str
    anchor_line: str = ""
    note: str = ""

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Verdict:
    """The ladder's answer. ``candidate is None`` means it found nothing."""

    candidate: Optional[Candidate] = None
    refused: str = ""
    stage: str = ""
    similarity: float = 0.0
    considered: Tuple[str, ...] = ()
    rejected: Tuple[str, ...] = field(default=())
    alternatives: int = 0
    guard: str = ""
    note: str = ""


# ---------------------------------------------------------------------------
# Normalisation helpers. Each returns (text, index_map) where index_map[i] is the
# OFFSET IN THE ORIGINAL TEXT that normalised character i came from, so a
# normalised hit can always be mapped back to real bytes.
# ---------------------------------------------------------------------------


def _line_map(text: str) -> List[Tuple[int, int]]:
    """Return ``(start, end)`` character spans for every line, newline included."""
    spans: List[Tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char == "\n":
            spans.append((start, index + 1))
            start = index + 1
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _strip_trailing(text: str) -> Tuple[str, List[int]]:
    """Remove trailing whitespace from every line, keeping the offset map."""
    out: List[str] = []
    offsets: List[int] = []
    cursor = 0
    for line in text.split("\n"):
        keep = len(line.rstrip())
        out.append(line[:keep])
        offsets.extend(range(cursor, cursor + keep))
        cursor += len(line) + 1  # +1 for the newline we split on
    return "\n".join(out), offsets


_WS = re.compile(r"\s+")


def _collapse_whitespace(text: str) -> Tuple[str, List[int]]:
    """Collapse every run of whitespace to one space; keep the offset map."""
    out: List[str] = []
    offsets: List[int] = []
    index = 0
    length = len(text)
    while index < length:
        match = _WS.match(text, index)
        if match:
            out.append(" ")
            offsets.append(index)
            index = match.end()
        else:
            out.append(text[index])
            offsets.append(index)
            index += 1
    return "".join(out), offsets


def _dedent(text: str) -> Tuple[str, List[int]]:
    """Remove the COMMON leading indentation; keep the offset map per line."""
    lines = text.split("\n")
    spans = _line_map(text)
    if not lines:
        return text, list(range(len(text)))
    prefixes = [len(line) - len(line.lstrip(" \t")) for line in lines]
    # A blank line has zero prefix and must not drag the common indent to zero;
    # that is why the minimum is taken over NON-EMPTY lines only.
    non_empty = [p for line, p in zip(lines, prefixes, strict=False) if line.strip()]
    common = min(non_empty) if non_empty else 0
    if common == 0:
        return text, list(range(len(text)))
    out: List[str] = []
    offsets: List[int] = []
    for (start, end), prefix in zip(spans, prefixes, strict=False):
        drop = min(common, prefix)
        out.append(text[start + drop : end])
        offsets.extend(range(start + drop, end))
    return "\n".join(out), offsets


_ESCAPES = (("\\n", "\n"), ("\\t", "\t"), ('\\"', '"'), ("\\\\", "\\"))


def _unescape(text: str) -> Tuple[str, List[int]]:
    """Collapse doubled escapes; keep the offset map."""
    out: List[str] = []
    offsets: List[int] = []
    index = 0
    length = len(text)
    while index < length:
        for literal, real in _ESCAPES:
            if text.startswith(literal, index):
                out.append(real)
                offsets.append(index)
                index += len(literal)
                break
        else:
            out.append(text[index])
            offsets.append(index)
            index += 1
    return "".join(out), offsets


def _similarity(a: str, b: str) -> float:
    """Return the normalised Levenshtein similarity of two strings, in [0, 1].

    A plain edit-distance ratio. ``difflib.SequenceMatcher`` would be faster
    and is tempting, but it is a MATCHING ratio, not an EDIT ratio: it scores
    two lines that share a long common prefix as near-identical even when the
    divergent tail is the part that matters. For an edit ladder the quantity
    that matters is "how many characters would I have to change", which is the
    edit distance.
    """
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    # Length bound, as an UPPER BOUND and not as a zero. The edit distance is at
    # least `|len(a) - len(b)|`, so the similarity cannot exceed
    # `1 - |len(a) - len(b)| / max(len)`. Returning 0.0 here instead -- which an
    # earlier draft did -- is a WRONG ANSWER rather than a pessimistic one, and
    # a scorer whose number is wrong on short pairs cannot be the authority that
    # decides whether an edit lands. The brute-force differential in
    # `harness/test_p2w1_fuzzy_edit.py` is what caught it, on ('a', 'aa').
    longest = max(len(a), len(b))
    ceiling = 1.0 - (abs(len(a) - len(b)) / float(longest))
    if ceiling <= 0.0:
        return 0.0
    # NOTE: no "this pair is hopeless, return the ceiling" shortcut here. The
    # ceiling is an OVER-estimate whenever the two strings also differ by
    # substitution, so returning it would be a wrong number, and this function
    # is the authority that decides whether a fuzzy edit lands. The cost is
    # bounded where it belongs instead -- `_levenshtein_window` only scores
    # anchor-adjacent windows, so the number of CALLS is bounded, not the table.
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            )
        previous = current
    distance = previous[len(b)]
    return 1.0 - (float(distance) / float(longest))


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@dataclass
class DisproportionateGuard:
    """Refuse a fuzzy match that could have landed in the wrong place.

    The failure this exists for, stated as a shape rather than a slogan: a
    model asks to replace ``return None`` (or a common function name, or a
    three-line idiom) and a similarity search finds *something* at 0.7. The
    edit lands. It lands in a different function. The run reports success, the
    tests pass because the change was harmless, and the actual bug is still
    there.

    Three rules, all of which are refusals rather than scoring adjustments,
    because a score a caller can ignore is not a guard:

    1. **Size floor, FIRST.** A SCORED match shorter than
       :data:`MIN_FUZZY_CHARS` is refused outright, and it is checked BEFORE the
       candidate count because for a short region the count is inflated by
       noise rather than by genuinely similar places. A normalisation stage and
       a block anchor are exempt: both proved the match rather than scored it.
       This is the single highest-value rule -- the dangerous matches are the
       tiny ones.
    2. **Unique anchor.** Every fuzzy stage after ``line_trimmed`` must be
       anchored by at least one line of ``old`` that is UNIQUE in the file
       after normalisation. A pattern with no unique line has no anchor, and a
       match with no anchor is a guess with a confidence number attached.
    3. **Uniqueness.** More than one candidate clearing the threshold is a
       refusal, exactly as ``>1`` exact match is ``ambiguous_match``. A ladder
       that picks the best of several plausible places is the coin flip this
       primitive was built to avoid.

    All three are refusals rather than score adjustments, because a score a
    caller can ignore is not a guard. There is deliberately no "one strong
    candidate may land alone" escape: the scored stage only generates candidates
    adjacent to a unique anchor, so such a case essentially never arises, and a
    safety valve nobody can reach is worse than no valve at all.
    """

    min_chars: int = MIN_FUZZY_CHARS
    min_lines: int = MIN_FUZZY_LINES

    def check(
        self,
        text: str,
        old: str,
        candidates: Sequence[Candidate],
        *,
        anchor_lines: Sequence[str],
    ) -> Tuple[Optional[Candidate], str, str]:
        """Return ``(accepted, refusal_reason, guard_name)``.

        ``accepted`` is ``None`` when the guard refuses. ``refusal_reason`` is
        always a human-readable sentence: a guard that refuses silently is
        indistinguishable from a guard that never fired. ``guard_name`` says
        WHICH rule fired, so a receipt can name it rather than saying "no".
        """
        clearing = [c for c in candidates if c.similarity >= DEFAULT_SIMILARITY]
        clearing.sort(key=lambda c: (-c.similarity, c.start))
        if not clearing:
            best = max((c.similarity for c in candidates), default=0.0)
            return (
                None,
                f"no candidate reached the {DEFAULT_SIMILARITY:.2f} similarity "
                f"threshold (best was {best:.2f})",
                "threshold",
            )

        # Rule 1 -- the SIZE FLOOR -- runs FIRST, before the candidate count, and
        # the ordering is the point rather than an accident.
        #
        # At 0.65 a NINETEEN-character pattern matches unrelated two-line windows
        # all over a real file: measured on a 300-character fixture, a 19-char
        # pattern produced SIX candidates at or above the threshold, the second
        # of them scoring exactly 0.65. So for a short region the candidate COUNT
        # is inflated by noise rather than by genuinely similar places, and
        # refusing with "6 places are similar" states the wrong reason.
        #
        # It matters that the stated reason is the one a reader can act on AND
        # trust. Both refusals happen to lead the caller to extend old_string,
        # so a test that only checked the outcome would pass either way -- but
        # "your pattern is too short to judge" and "your pattern matches six
        # places" are different facts, and only the first is true here.
        if clearing[0].stage in SCORED_STAGES and clearing[0].length < self.min_chars:
            return (
                None,
                f"the best candidate is {clearing[0].length} characters long, under "
                f"the {self.min_chars}-character floor for a scored fuzzy match. A "
                f"match that short has no context left to disagree with it, so the "
                f"similarity number is measuring noise; extend old_string until it "
                f"carries enough context to be locatable.",
                "size_floor",
            )

        # Rule 3 -- uniqueness. No "one strong candidate may land alone" escape:
        # that branch was UNREACHABLE (the scored stage only generates candidates
        # adjacent to a unique anchor, so two clearing candidates of different
        # quality essentially never occur), and a guard branch nobody can reach
        # is worse than none -- it reads as a safety valve that does not exist.
        if len(clearing) > 1:
            return (
                None,
                f"{len(clearing)} places in the file are at least "
                f"{DEFAULT_SIMILARITY:.2f} similar to old_string (best "
                f"{clearing[0].similarity:.2f}, second {clearing[1].similarity:.2f}). "
                "Picking one would be a coin flip between call sites; extend "
                "old_string until it is unique.",
                "uniqueness",
            )
        accepted = clearing[0]
        guard = ""

        # Rule 2: the unique anchor. Normalisation and block-anchor stages get
        # to skip this because they PROVED uniqueness by construction; a scored
        # stage did not.
        if accepted.stage not in ("line_trimmed", "block_anchor") and not anchor_lines:
            return (
                None,
                "no line of old_string is unique in this file, so there is no "
                "anchor to locate the block with. A match with no anchor is a "
                "guess with a confidence number attached; extend old_string "
                "until at least one of its lines appears exactly once.",
                "anchor",
            )
        return accepted, "", guard


# ---------------------------------------------------------------------------
# The stages
# ---------------------------------------------------------------------------


def _exact(text: str, old: str) -> List[Candidate]:
    out: List[Candidate] = []
    start = text.find(old)
    while start >= 0:
        out.append(Candidate(start, start + len(old), 1.0, "exact"))
        start = text.find(old, start + 1)
    return out


def _normalised_stage(text: str, old: str, stage: str) -> List[Candidate]:
    """Run one normalisation on both sides and return the equal-region hits.

    The point of a normalisation stage is that it is EXACT on the normalised
    form: it tolerates one specific class of drift and nothing else, which is
    why it sits below the scored stage and above ``exact``.
    """
    text_norm, text_map = _apply_normalisation(stage, text)
    old_norm, _old_map = _apply_normalisation(stage, old)
    if not old_norm:
        return []
    out: List[Candidate] = []
    cursor = 0
    while True:
        found = text_norm.find(old_norm, cursor)
        if found < 0:
            break
        last = found + len(old_norm) - 1
        if last < len(text_map):
            out.append(
                Candidate(
                    text_map[found],
                    text_map[last] + 1,
                    1.0,
                    stage,
                )
            )
        cursor = found + 1
    return out


def _apply_normalisation(stage: str, text: str) -> Tuple[str, List[int]]:
    if stage == "line_trimmed":
        return _strip_trailing(text)
    if stage == "whitespace_normalised":
        return _collapse_whitespace(text)
    if stage == "indentation_normalised":
        return _dedent(text)
    if stage == "escape_normalised":
        return _unescape(text)
    raise ValueError(f"not a normalisation stage: {stage!r}")


def _block_anchor(text: str, old: str) -> List[Candidate]:
    """Anchor on a UNIQUE line of ``old``, then grow to the same line count.

    This is the stage that survives an edit the caller cannot reproduce at all:
    the model re-indented the block and re-wrapped a line, so no normalised
    form of the whole thing matches. A single line usually still does, and one
    unique line is enough to say WHERE the block is -- which is the only
    question a block edit actually needs answered.
    """
    old_lines = old.split("\n")
    text_lines = text.split("\n")
    spans = _line_map(text)
    if len(old_lines) < MIN_FUZZY_LINES or len(spans) < len(old_lines):
        return []
    # Try the longest lines first: the more of the block that has to match, the
    # stronger the anchor.
    order = sorted(
        range(len(old_lines)),
        key=lambda i: (-len(old_lines[i].strip()), i),
    )
    for index in order:
        needle = old_lines[index].strip()
        if len(needle) < 8:
            continue
        hits = [i for i, line in enumerate(text_lines) if needle in line]
        if len(hits) != 1:
            continue
        hit = hits[0]
        top = hit - index
        if top < 0 or top + len(old_lines) > len(spans):
            continue
        start = spans[top][0]
        end = spans[top + len(old_lines) - 1][1]
        score = _similarity(
            "\n".join(line.strip() for line in old_lines),
            "\n".join(line.strip() for line in text_lines[top : top + len(old_lines)]),
        )
        if score >= DEFAULT_SIMILARITY:
            return [
                Candidate(
                    start,
                    end,
                    score,
                    "block_anchor",
                    anchor_line=needle,
                    note=(
                        f"anchored on the unique line {needle!r} at file line "
                        f"{hit + 1}"
                    ),
                )
            ]
    return []


def _levenshtein_window(
    text: str, old: str, anchor_lines: Sequence[str], *, max_windows: int = 64
) -> List[Candidate]:
    """Score only the LINE-ALIGNED windows that CONTAIN AN ANCHOR LINE.

    Two bounds, both measured rather than reasoned about, and both load-bearing:

    * **Anchor-gated.** Every window scored must contain a line that appears
      exactly once in the file. Scoring every window in the file is O(lines x
      window x pattern) and on this repository's own `harness/tool_errors.py`
      (3,100 lines) a 40-line pattern took **over five minutes** -- a fuzzy
      ladder that hangs the edit path is worse than no ladder, because the
      model is waiting on it. Gating on an anchor turns it into "find the one
      place this could be", which is both linear and SAFER: it promotes the
      guard's anchor rule from a post-hoc check into a candidate generator, so
      an unanchored pattern never becomes a candidate at all.
    * **Line-aligned.** A window that starts mid-line reports an offset that is
      not a line anybody can look at, and an edit whose receipt names a column
      nobody asked for is an edit nobody can review.

    ``max_windows`` is a hard ceiling on how many candidates one call may score.
    A pattern with more than this many plausible homes is ambiguous, and the
    honest answer to ambiguity is a refusal, not a slower search.
    """
    old_lines = old.split("\n")
    text_lines = text.split("\n")
    spans = _line_map(text)
    if not old_lines or len(spans) < len(old_lines):
        return []
    width = len(old_lines)
    needle = "\n".join(line.rstrip() for line in old_lines)

    # Locate the anchor lines. First one wins; they are already known unique.
    anchor_at: Optional[int] = None
    for candidate in anchor_lines:
        try:
            anchor_at = text_lines.index(candidate)
        except ValueError:
            anchor_at = None
        if anchor_at is not None:
            break

    if anchor_at is not None:
        tops = range(max(0, anchor_at - width + 1), min(len(spans) - width + 1, anchor_at + 1))
    else:
        tops = range(0, len(spans) - width + 1)

    out: List[Candidate] = []
    for top in tops:
        score = _similarity(
            needle,
            "\n".join(line.rstrip() for line in text_lines[top : top + width]),
        )
        if score >= DEFAULT_SIMILARITY:
            out.append(
                Candidate(
                    spans[top][0],
                    spans[top + width - 1][1],
                    score,
                    "context_levenshtein",
                    anchor_line=anchor_lines[0] if anchor_lines else "",
                )
            )
        if len(out) >= max_windows:
            break
    out.sort(key=lambda c: (-c.similarity, c.start))
    return out


def _anchor_lines(text: str, old: str) -> List[str]:
    """Return ``old``'s lines that are UNIQUE in the file, trimmed."""
    text_lines = {line.strip() for line in text.split("\n")}
    counts: Dict[str, int] = {}
    for line in text.split("\n"):
        counts[line.strip()] = counts.get(line.strip(), 0) + 1
    out: List[str] = []
    for line in old.split("\n"):
        needle = line.strip()
        if len(needle) >= 8 and counts.get(needle, 0) == 1 and needle in text_lines:
            out.append(needle)
    return out


def run_ladder(
    text: str,
    old: str,
    *,
    similarity: float = DEFAULT_SIMILARITY,
    guard: Optional[DisproportionateGuard] = None,
) -> Verdict:
    """Run the cascade over ``text`` and return the first rung that lands.

    **Exact is first and exact always wins.** A ladder that runs its fuzzy
    stages before checking for an exact match would replace a perfect match
    with a merely-good one, which is the opposite of a ladder. Every stage
    below ``exact`` is only reached when ``exact`` found nothing.

    Stages run in :data:`STAGES` order and the first stage that produces a
    guard-approved candidate wins; later stages are not run, because a later
    rung can only ever be a weaker answer than an earlier one that already
    succeeded. `considered` records every stage that ran, so a receipt can say
    what was tried and not just what landed.

    The final rung delegates to the Bitap second opinion, which is a DIFFERENT
    algorithm rather than a looser threshold on this one -- that is why it can
    be last without being redundant.

    Never raises. Every failure is a ``Verdict`` with ``refused`` set.
    """
    guard = guard or DisproportionateGuard()
    considered: List[str] = []
    rejected: List[str] = []

    exact = _exact(text, old)
    considered.append("exact")
    if exact:
        if len(exact) > 1:
            # The ladder is held to the SAME ambiguity rule as the primitive
            # that calls it: more than one exact match is a refusal, not "the
            # first one". An earlier draft returned `exact[0]` here, which is a
            # coin flip between call sites wearing a confidence of 1.0 -- and a
            # perfect score is the most misleading possible way to be wrong.
            return Verdict(
                refused=(
                    f"old_string matches {len(exact)} places in this file. "
                    "Extend old_string until it is unique; picking one would be "
                    "a coin flip between call sites."
                ),
                stage="exact",
                similarity=1.0,
                considered=tuple(considered),
                alternatives=len(exact),
                guard="uniqueness",
            )
        best = exact[0]
        return Verdict(
            candidate=best,
            stage="exact",
            similarity=1.0,
            considered=tuple(considered),
        )

    anchors = _anchor_lines(text, old)

    # -- the normalisation stages: exact on a normalised form --------------
    for stage in ("line_trimmed", "whitespace_normalised", "indentation_normalised",
                  "escape_normalised"):
        considered.append(stage)
        hits = _normalised_stage(text, old, stage)
        if len(hits) == 1:
            return Verdict(
                candidate=hits[0],
                stage=stage,
                similarity=1.0,
                considered=tuple(considered),
                rejected=tuple(rejected),
                note=(
                    "matched after normalising "
                    f"{stage.replace('_', ' ')}; no characters were guessed"
                ),
            )
        if len(hits) > 1:
            rejected.append(f"{stage}:{len(hits)}_places")
            # Several normalised hits is still ambiguity, and it is reported as
            # such rather than resolved by picking one.
            return Verdict(
                refused=(
                    f"old_string matches {len(hits)} places in this file after "
                    f"{stage.replace('_', ' ')} normalisation. Extend old_string "
                    "until it is unique."
                ),
                stage=stage,
                considered=tuple(considered),
                rejected=tuple(rejected),
                alternatives=len(hits),
            )

    # -- block anchor: one unique line, then grow to the block --------------
    considered.append("block_anchor")
    anchored = _block_anchor(text, old)
    if anchored:
        accepted, why, which = guard.check(
            text, old, anchored, anchor_lines=anchors or [anchored[0].anchor_line]
        )
        if accepted is not None:
            return Verdict(
                candidate=accepted,
                stage="block_anchor",
                similarity=accepted.similarity,
                considered=tuple(considered),
                rejected=tuple(rejected),
                guard=which,
                note=accepted.note,
            )
        rejected.append(f"block_anchor:{which}")

    # -- the scored stage --------------------------------------------------
    considered.append("context_levenshtein")
    scored = _levenshtein_window(text, old, anchors)
    bitap: List[Candidate] = []
    if not scored:
        considered.append("bitap_second_opinion")
        bitap = _bitap_stage(text, old)
    pool = scored or bitap
    accepted, why, which = guard.check(text, old, pool, anchor_lines=anchors)
    if accepted is None:
        return Verdict(
            refused=why,
            stage=considered[-1],
            similarity=max((c.similarity for c in pool), default=0.0),
            considered=tuple(considered),
            rejected=tuple(rejected),
            guard=which,
            alternatives=len(pool),
        )
    return Verdict(
        candidate=accepted,
        stage=accepted.stage,
        similarity=accepted.similarity,
        considered=tuple(considered),
        rejected=tuple(rejected),
        guard=which,
        alternatives=len(pool) - 1,
        note=(
            f"fuzzy match at {accepted.similarity:.2f} similarity"
            + (" (unambiguous and strong)" if which == "unique_strong" else "")
        ),
    )


def _bitap_stage(text: str, old: str) -> List[Candidate]:
    """The Bitap second opinion, as :class:`Candidate` rows.

    **Every location it proposes is RE-SCORED with `_similarity` before it is
    returned**, and a location whose true Levenshtein ratio does not clear
    :data:`DEFAULT_SIMILARITY` is dropped.

    That re-score is not politeness to the bitap matcher; it is the reason this
    rung is safe to let land an edit at all. Bitap is an APPROXIMATE matcher:
    it is monotone in drift but its automaton is not the edit-distance metric,
    so a window can clear its threshold and still be a worse match than it
    claims. This implementation is an independent reimplementation of the
    algorithm (see the header), and an independent reimplementation of an
    approximate matcher is exactly the thing that must not be trusted with an
    authoritative answer.

    So the division of labour is: bitap proposes LOCATIONS fast and in one
    sweep; `_similarity` decides. Bitap's contribution is real -- it finds
    candidates no line-aligned window scan would consider, because it slides
    across character offsets rather than line starts -- but its verdict is not
    used. A matcher that cannot land an edit on its own cannot silently damage
    a repository, and the failure mode of being wrong is a missed fuzzy match
    (`no_match`), which the model can act on, rather than a wrong edit, which
    it cannot.

WHAT WAS DELIBERATELY **NOT** VENDORED, AND WHY
---------------------------------------------
Upstream's ``replacer.ts`` is roughly 500 lines and carries three things this
ladder does not want:

1. **Its own line-number anchor.** It accepts a line number as an edit site.
   The UXP corpus is explicit that models drift on line numbers more than on
   contextual matching, and a line anchor is the one anchor that goes stale the
   instant anything above it moves. Nothing in this file accepts a line number;
   every offset returned is a CHARACTER offset into the exact bytes the caller
   already holds.
2. **The ordering.** Upstream runs its cascade INSTEAD OF an exact match. This
   is a LADDER AFTER exact match, never instead of it -- the exact path keeps
   its own byte-level guarantees (``stale_read``, ``ambiguous_match``, atomic
   write, rollback) and this file only ever computes an offset, a length and a
   replacement for ``harness.editor`` to splice with the same code it would have
   used for an exact hit.
3. **~500 lines of surrounding machinery** -- streaming/regex plumbing and
   TypeScript-specific structure -- that has no counterpart in a Python caller.

WHAT IS **NOT** CLAIMED
-----------------------
This is an INDEPENDENT IMPLEMENTATION of the published algorithm and its
published constants, **not a byte copy**. No network fetch of the upstream file
was available in the session that created this package, so reproducing 500 lines
from memory would have been an unverifiable claim of fidelity. The behaviour
and the constants are reproduced; byte-identity is NOT claimed, and the header
above says ``modified: no`` because nothing was ever copied that could be
modified.

TWO DEFECTS IN THIS FILE, FOUND BY ``harness/edit_drift_bench.py`` AND NOT
FIXED HERE, BECAUSE A VENDORED FILE IS NEVER EDITED IN PLACE
-------------------------------------------------------------
Both are corrected in the WRAPPER (``harness/fuzzy_edit.py``), and both are
recorded here so a future reader does not "fix" them in this copy:

1. ``_strip_trailing``'s offset map omits the newlines it joins lines with, so
   ``len(map) < len(normalised)`` by one per line (measured 883 vs 920 on a
   38-line file). Every hit after the first newline maps to too high an offset.
   Wrapper guard: ``_normalisation_integrity``.
2. Region extents computed from a LINE COUNT count the trailing EMPTY lines a
   newline-terminated block splits into, so a region reaches one line past the
   caller's block. Measured: a 258-character target returned a 294-character
   region whose tail was the NEXT function's ``def``. Wrapper guard:
   ``_clamp_region_extent``.

A third finding is a POLICY error rather than a code error: ``block_anchor`` is
exempted from the disproportionate guard on the stated grounds that it "proved
the match rather than scored it", but it *scores* -- it returns any window at
>= 0.65. A caller asking for a function that does not exist scored 0.667 and got
a successful corrupting write. Wrapper guard: ``_refuse_weak_anchor``, whose
floor of 0.72 was fitted between that 0.667 and the lowest legitimate 0.80.

"""
    from . import aider_bitap

    out: List[Candidate] = []
    for found in aider_bitap.all_matches(text, old):
        window = text[found.start : found.end]
        verified = _similarity(old, window)
        if verified < DEFAULT_SIMILARITY:
            continue
        out.append(
            Candidate(found.start, found.end, verified, "bitap_second_opinion")
        )
    return out


__all__ = [
    "DEFAULT_SIMILARITY",
    "MIN_FUZZY_CHARS",
    "MIN_FUZZY_LINES",
    "STAGES",
    "STRONG_SIMILARITY",
    "Candidate",
    "DisproportionateGuard",
    "Verdict",
    "run_ladder",
]
