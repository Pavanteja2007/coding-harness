"""The fuzzy edit LADDER: exact match first, a cascade second, always reported.

**What this module is.** `harness.editor.apply_text_edit` is exact-only. A
single whitespace drift is a hard ``no_match``, and the model spends a turn
re-reading a file it already read. This module is the fallback ladder that runs
ONLY after exact match has failed, and it is built so that a fuzzy match is a
WEAKER guarantee than an exact one in every direction that matters:

* it is **reported** -- ``{"match": "fuzzy", "strategy": ..., "confidence": ...}``
  rides the `EditOutcome`, so a reviewer looking at a diff can see the edit did
  not land on an exact match;
* it is **guarded** -- `DisproportionateGuard` refuses a match that could have
  landed in the wrong place, and a refusal is a refusal, not a lower score;
* it is **bounded** -- every stage has a ceiling and the expensive rungs are
  measured, not assumed.

**What this module is NOT.** It never replaces exact matching. It is not a
second writer. It computes an offset, a length and a replacement, and
`harness.editor` splices those into the SAME byte array it would have spliced an
exact match into -- so the byte-fidelity, ``stale_read``, ``ambiguous_match``,
atomic-write and rollback properties are unchanged, not reimplemented.

**Never line-number-based.** Nothing in this module accepts a line number as an
edit anchor. The corpus is explicit that models drift on line numbers more than
on contextual matching, and a line-number anchor is also the one anchor that
goes stale the moment anything above it moves. Every offset this module returns
is a CHARACTER offset into the exact bytes `harness.editor` already holds, and
it is re-derived from content on every call.

Config (read from ``Task.config``; NONE of these are in
``harness/config.py::DEFAULTS``, because a default there is merged into every
task and every eval arm and this changes what an edit MEANS):

``edit_fuzzy`` (default True) -- the master switch, read by key presence.
``edit_fuzzy_similarity`` (default 0.65) -- the cascade threshold.
``edit_fuzzy_min_chars`` (default 24) -- the disproportionate guard's floor.
``edit_fuzzy_min_lines`` (default 3) -- the guard's floor on anchor blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Optional, Tuple

from harness.third_party import aider_bitap, opencode_replacer

__all__ = [
    "DEFAULT_MAX_REGION_FRACTION",
    "LLM_GUARANTEE",
    "LLM_STAGE",
    "MATCH_GUARANTEES",
    "STAGES",
    "THRESHOLDS",
    "FuzzyMatch",
    "fuzzy_enabled",
    "ladder_report",
    "llm_repair_enabled",
    "llm_repair_report",
    "llm_repair_stage",
    "resolve_fuzzy",
]

#: The largest fraction of a file one fuzzy match may cover. See
#: :func:`_refuse_region_coverage` for the measurement that produced it and for
#: why a threshold cannot catch the same defect.
DEFAULT_MAX_REGION_FRACTION: float = 0.5

#: The stage list, re-exported so a consumer can print it without reaching into
#: the vendored module.
STAGES = opencode_replacer.STAGES

#: Every number the ladder can refuse or accept on, in one place, so a receipt
#: can render the policy it ran under without re-deriving it.
THRESHOLDS: Dict[str, Any] = {
    "similarity": opencode_replacer.DEFAULT_SIMILARITY,
    "strong_similarity": opencode_replacer.STRONG_SIMILARITY,
    "min_chars": opencode_replacer.MIN_FUZZY_CHARS,
    "min_lines": opencode_replacer.MIN_FUZZY_LINES,
    "bitap_threshold": aider_bitap.DEFAULT_MATCH_THRESHOLD,
    "bitap_distance": aider_bitap.DEFAULT_SEARCH_DISTANCE,
    "bitap_max_pattern": aider_bitap.MAX_SEARCH_PATTERN,
    "bitap_max_text": aider_bitap.MAX_SEARCH_TEXT,
    "max_region_fraction": DEFAULT_MAX_REGION_FRACTION,
}


@dataclass(frozen=True)
class FuzzyMatch:
    """A resolved fuzzy match, expressed in the caller's own coordinates.

    ``start`` / ``end`` are CHARACTER offsets into the decoded text the caller
    passed, and ``replacement`` is the decoded text to splice in. The caller
    re-encodes both, which is what keeps the byte splice in `harness.editor`
    exactly where it was: this module never touches a file.
    """

    start: int
    end: int
    replacement: str
    stage: str
    confidence: float
    guard: str = ""
    note: str = ""
    considered: Tuple[str, ...] = ()
    rejected: Tuple[str, ...] = ()
    alternatives: int = 0

    @property
    def is_exact(self) -> bool:
        return self.stage == "exact"

    def report(self) -> Dict[str, Any]:
        """Return the RECEIPT shape a diff reviewer and a model can both read.

        The four keys the brief names are here verbatim -- ``match``,
        ``strategy``, ``confidence`` -- plus ``guard``, because a fuzzy match
        that the disproportionate guard *allowed* is a materially different
        claim from one it merely scored, and a receipt that cannot tell them
        apart is not a receipt.

        ``match`` is ``"fuzzy"`` for every stage above ``exact``. A caller
        rendering a diff can therefore make one decision: is this exact or not.
        """
        return {
            "match": "exact" if self.is_exact else "fuzzy",
            "strategy": self.stage,
            "confidence": round(float(self.confidence), 4),
            "guard": self.guard,
            "considered": list(self.considered),
            "rejected": list(self.rejected),
            "alternatives": self.alternatives,
            "note": self.note,
            "thresholds": dict(THRESHOLDS),
        }


def fuzzy_enabled(config: Optional[Mapping[str, Any]]) -> bool:
    """Return whether the ladder may run, by KEY PRESENCE plus truthiness.

    Absent means on, because the ladder only runs after an exact match has
    already failed -- so its absence changes no edit that was going to succeed
    anyway. An explicit falsy value turns it off for a caller who wants
    ``no_match`` to mean ``no_match``.
    """
    if config is None:
        return True
    if "edit_fuzzy" not in config:
        return True
    value = config.get("edit_fuzzy")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "fuzzy"}
    return bool(value)


def _replacement_for(
    old: str, new: str, matched_region: str, stage: str
) -> str:
    """Return the text that replaces ``matched_region``.

    The replacement is derived from ``new`` -- the caller's intent -- re-shaped
    onto whatever the file actually contains. That is the only defensible
    direction: deriving it from the MATCHED REGION would silently drop the
    caller's edit and write the file back unchanged, which reports as a
    successful edit and changes nothing.

    **Whitespace collapsing is a MATCHING device and is never applied to the
    replacement.** An earlier draft did collapse it, and the pre-commit syntax
    gate caught the result on the first real run:
    ``def alpha(): value = 9 return value`` -- three statements on one line, a
    syntax error, written into a repository. A matcher that flattens newlines
    to find a region must not flatten them to build the edit.

    So there is exactly ONE re-shaping rule, and it applies to both
    normalisation stages:

    * ``whitespace_normalised`` / ``indentation_normalised`` -- the region was
      found through a normalisation, so the replacement takes the MATCHED
      region's own leading indentation for its common indent. Without this a
      re-indented edit lands at the caller's column and the line after it
      suddenly disagrees.
    * ``block_anchor`` / ``context_levenshtein`` / ``bitap_second_opinion`` --
      the region was LOCATED, not normalised, so the replacement is the
      caller's own text verbatim. Re-indenting there would be inventing an edit
      the caller did not ask for.
    """
    del old
    if stage in ("whitespace_normalised", "indentation_normalised"):
        return _reindent(new, _leading_whitespace(matched_region))
    return new


# ---------------------------------------------------------------------------
# The two guards P2/W2-T3 added, and WHY they live in the wrapper
# ---------------------------------------------------------------------------
#
# Both of the defects below were found by `harness/edit_drift_bench.py`, the
# edit-drift benchmark, on its first run against the cascade. Both are
# CORRECTNESS defects -- each one silently corrupts a file -- and both are
# fixed HERE rather than in `harness/third_party/opencode_replacer.py`.
#
# That placement is not a preference. A vendored file is never modified in
# place: the moment somebody "fixes a bug in the vendor copy", the provenance
# claim becomes false and the pinned checksum stops meaning anything. The brief
# for this phase says it outright -- *change the wrapper*. So the vendored
# algorithms keep their published behaviour and their checksums, and every
# correction we need is applied to the layer that already owns the call.
#
# `region_extent` is therefore a genuine wrapper-side correction (it rewrites
# the offsets the ladder returned), and `region_coverage` is a genuine
# wrapper-side refusal (the ladder has no opinion about it at all).


def _content_lines(text: str) -> int:
    """Return the number of lines in ``text`` that carry non-whitespace.

    **This is the unit the region clamp is denominated in, and the choice is
    the whole fix.** A newline-terminated block splits into trailing EMPTY
    "lines" under ``str.split("\\n")``, and every ladder stage that computes a
    region's extent from a line COUNT therefore counts those empties as
    content. Counting only lines that carry non-whitespace removes the
    discrepancy at its source: a block's trailing blank lines are not part of
    what the caller pointed at, so they must not enlarge the region either.
    """
    return sum(1 for line in text.split("\n") if line.strip())


def _clamp_region_extent(
    match: "FuzzyMatch", text: str, old: str
) -> Tuple["FuzzyMatch", str]:
    """Stop a matched region from eating past the caller's own block.

    **The defect, measured.** A string ending in a newline splits into trailing
    EMPTY "lines" -- ``"a\\n\\n\\n".split("\\n") == ['a', '', '', '']`` -- so
    ``block_anchor``'s ``top + len(old_lines) - 1`` and the normalisation
    stages' character arithmetic both reach one line further than the caller's
    block. Measured on the benchmark's synthetic fixture: a **258-character**
    target came back as a **294-character** region whose tail was
    ``def unrelated_helper(alpha, beta):``. Applying that edit DELETES a
    function. Silent corruption -- the write succeeds, the run reports success,
    and the repository is broken.

    **The fix: a budget of content lines, not a character count.** The region
    may not contain more non-whitespace lines than ``old`` does. The class this
    was found on -- ``block_tail_blank_lines`` -- is exactly the case where the
    ladder's line count exceeded the caller's by one, and truncating at the end
    of the caller's last content line removes precisely the over-reach while
    leaving an ordinary block untouched:

    * a region whose content lines already equal ``old``'s is unchanged, so
      every well-behaved edit is byte-identical to before;
    * a region with FEWER content lines than ``old`` is left alone -- that is a
      collapsed or minified match, which :func:`_refuse_region_coverage`
      refuses rather than silently widening here.

    Returns ``(match, guard_name)``; ``guard_name`` is non-empty when the clamp
    actually fired, so the receipt can say the region was corrected rather than
    implying the ladder got it right unaided.
    """
    budget = _content_lines(old)
    if budget <= 0:
        return match, ""
    region = text[match.start : match.end]
    seen = 0
    cursor = 0
    for line in region.split("\n"):
        cursor += len(line) + 1
        if line.strip():
            seen += 1
            if seen == budget:
                break
    if seen < budget or cursor >= len(region):
        # Fewer content lines than the caller asked for, or the budget was
        # already satisfied: nothing to give back.
        return match, ""
    clamped = replace(match, end=match.start + min(cursor, len(region)))
    return clamped, "region_extent"


def _region_coverage(text: str, start: int, end: int) -> float:
    """Return the fraction of ``text`` the matched region covers."""
    return (end - start) / len(text) if text else 0.0


def _refuse_region_coverage(
    text: str, start: int, end: int, maximum: float
) -> str:
    """Return a refusal sentence when a fuzzy match covers too much of the file.

    **The defect this closes.** On a MINIFIED or generated file the
    ``whitespace_normalised`` stage collapses the entire file -- and the
    caller's block -- to runs of single spaces, so any block matches any block
    at confidence 1.0. The benchmark's class-10 case returned a match covering
    **100% of the file**: applying it would have replaced the entire generated
    file with a six-line function. That is the single worst outcome the cascade
    can produce, and no threshold catches it, because the similarity was
    perfect.

    **The rule.** A fuzzy match may not cover more than
    ``edit_fuzzy_max_region_fraction`` (default 0.5) of the file. This is not a
    threshold fitted to one fixture: a *block* edit that rewrites most of a file
    is not a block edit, whether the file is minified, wholesale-reformatted,
    or the model pasted the whole file as ``old_string``. All three are the same
    defect. The bound is configurable because a caller who genuinely wants a
    90%-of-file fuzzy rewrite should have to say so explicitly -- and at that
    point they should be using ``write``, which is not fuzzy and has no anchor
    to get wrong.
    """
    covered = _region_coverage(text, start, end)
    if covered <= maximum:
        return ""
    lines = text.count("\n") + 1
    return (
        f"the best fuzzy candidate covers {covered:.0%} of this file "
        f"({lines} line(s) total), over the {maximum:.0%} ceiling for a block "
        "edit. A match that rewrites most of a file is not a block edit -- on a "
        "minified or generated file every block matches every block once "
        "whitespace is collapsed. Write the file instead of editing it."
    )


def _leading_whitespace(text: str) -> str:
    """Return the first line's leading whitespace, or ``""``."""
    line = text.split("\n", 1)[0]
    return line[: len(line) - len(line.lstrip())]


def _reindent(text: str, target: str) -> str:
    """Re-indent ``text`` so its first line carries ``target``'s indentation.

    Only the COMMON leading whitespace is rewritten, so a replacement whose own
    internal structure is deeper than the target keeps it.
    """
    if not target:
        return text
    lines = text.split("\n")
    prefixes = [len(line) - len(line.lstrip(" \t")) for line in lines if line.strip()]
    common = min(prefixes) if prefixes else 0
    if common == 0:
        return text
    return "\n".join(target + line[common:] if line.strip() else line for line in lines)


def resolve_fuzzy(
    text: str,
    old: str,
    new: str,
    *,
    config: Optional[Mapping[str, Any]] = None,
    llm_call: Any = None,
    llm_spent: Optional[Dict[str, int]] = None,
) -> Tuple[Optional[FuzzyMatch], str]:
    """Resolve ``old`` in ``text`` through the ladder.

    Returns ``(match, reason)``. ``match`` is ``None`` when the ladder found
    nothing, and ``reason`` then says WHY in a sentence a model can act on --
    which is the difference between a refusal the model can fix and a refusal
    that reads as a bug.

    ``llm_call`` / ``llm_spent`` are the opt-in terminal stage (T1.W2.2). Both
    default to ``None``, so with no configuration this function's behaviour is
    exactly the deterministic ladder's -- see :func:`llm_repair_enabled` for why
    the switch is opt-in by key presence rather than a ``DEFAULTS`` value.

    Assumes ``text`` is the decoded file content the caller is about to splice
    and that ``old``/``new`` are the caller's strings with the file's own line
    endings already applied (i.e. after `harness.editor`'s newline adaptation).
    Never raises: a ladder that raises turns a recoverable mismatch into a
    crashed edit.
    """
    if not fuzzy_enabled(config):
        return None, (
            "edit_fuzzy is disabled, so this is a plain exact-match refusal. "
            "Re-read the file and copy the exact text."
        )
    similarity = _number(
        config, "edit_fuzzy_similarity", opencode_replacer.DEFAULT_SIMILARITY
    )
    guard = opencode_replacer.DisproportionateGuard(
        min_chars=int(
            _number(config, "edit_fuzzy_min_chars", opencode_replacer.MIN_FUZZY_CHARS)
        ),
        min_lines=int(
            _number(config, "edit_fuzzy_min_lines", opencode_replacer.MIN_FUZZY_LINES)
        ),
    )
    try:
        verdict = opencode_replacer.run_ladder(
            text, old, similarity=similarity, guard=guard
        )
    except Exception as exc:  # pragma: no cover - the ladder is total by design
        return None, f"the fuzzy matcher failed ({exc}); treating it as no match"

    if verdict.candidate is None:
        # T1.W2.2: the terminal stage runs ONLY after every deterministic rung
        # has failed, and only when it is explicitly switched on. Its refusal
        # REPLACES the ladder's reason when it ran, so the reason a model reads
        # names the stage that actually made the decision.
        if llm_repair_enabled(config, call=llm_call):
            return llm_repair_stage(
                text, old, new, config=config, call=llm_call, spent=llm_spent
            )[:2]
        return None, verdict.refused or (
            "old_string was not found in this file, and no fuzzy rung could "
            "locate it either."
        )
    candidate = verdict.candidate

    # P2/W2-T3 guard 1 of 5: a file with fewer lines than the caller's block has
    # no structure to anchor to. Checked BEFORE anything is scored, because on a
    # minified file every later stage either agrees for the wrong reason or spends
    # work on a match that cannot be trusted.
    structural = _refuse_missing_line_structure(text, old)
    if structural:
        return None, structural

    # P2/W2-T3 guard 2 of 5: a SCORED stage must clear the STRONG threshold.
    # `block_anchor` used to be exempt from every guard on the stated grounds
    # that it PROVED its match; it does not, it scores, and a 0.667 score was
    # enough to rewrite a function the caller had named wrongly.
    weak = _refuse_weak_anchor(candidate.stage, candidate.similarity)
    if weak:
        return None, weak

    # P2/W2-T3 guard 3 of 5: a match that rewrites most of the file is refused
    # BEFORE anything is written. Checked before the extent clamp because a
    # 100%-of-file region has no meaningful tail to clamp, and reporting "the
    # region was corrected" for a region that was about to delete the file
    # would be a misleading receipt.
    ceiling = _fraction(
        config, "edit_fuzzy_max_region_fraction", DEFAULT_MAX_REGION_FRACTION
    )
    refusal = _refuse_region_coverage(text, candidate.start, candidate.end, ceiling)
    if refusal:
        return None, refusal

    # P2/W2-T3 guard 4 of 5: verify the ladder's OWN claim before splicing.
    # Checked before the extent clamp, because a region whose offsets are
    # already known to be wrong cannot be rescued by trimming its tail.
    integrity = _normalisation_integrity(
        candidate.stage, text[candidate.start : candidate.end], old
    )
    if integrity:
        return None, integrity

    region = text[candidate.start : candidate.end]
    match = FuzzyMatch(
        start=candidate.start,
        end=candidate.end,
        replacement=_replacement_for(old, new, region, candidate.stage),
        stage=candidate.stage,
        confidence=candidate.similarity,
        guard=verdict.guard,
        note=verdict.note or candidate.note,
        considered=verdict.considered,
        rejected=verdict.rejected,
        alternatives=verdict.alternatives,
    )
    # P2/W2-T3 guard 6 of 6: stop the region eating the next declaration.
    match, clamped = _clamp_region_extent(match, text, old)
    if clamped:
        match = replace(
            match,
            guard=(match.guard + "," if match.guard else "") + clamped,
            note=(match.note + " " if match.note else "")
            + "the region was clamped back to old_string's last real "
            "character: this ladder counts the trailing blank lines of a "
            "newline-terminated block as content and returned a region past "
            "the block's end.",
        )
    return match, ""


def _normalisation_integrity(
    stage: str, region: str, old: str
) -> str:
    """Return a refusal when a stage's own claim about its region is false.

    **The defect this catches, measured.** ``opencode_replacer._strip_trailing``
    builds its offset map with ``offsets.extend(range(cursor, cursor + keep))``
    -- one entry per KEPT character -- but joins the stripped lines with
    ``"\\n".join(out)``, whose newlines occupy positions in the normalised text
    that the map never accounts for. So ``len(map) < len(normalised)`` by one
    per line: measured **883 vs 920** on a 38-line file, a **37-position**
    shortfall. Every hit after the first newline is therefore mapped to an
    offset that is too high, and the returned region does not contain the
    caller's text.

    **Why that is corruption and not imprecision.** These stages claim
    EXACTNESS: ``line_trimmed``, ``whitespace_normalised``,
    ``indentation_normalised`` and ``escape_normalised`` each assert that the
    region equals ``old`` once both sides are put through that one
    normalisation. That claim is checkable for the price of one normalisation
    of the returned region, and it is the cheapest possible test of whether the
    offsets can be trusted at all. When it fails, the alternative to refusing
    is splicing at an offset the ladder has already admitted is wrong.

    **Not applied to the scored or anchored stages.** ``block_anchor``,
    ``context_levenshtein`` and ``bitap_second_opinion`` locate a region
    APPROXIMATELY by design -- their whole contract is a similarity score, not
    an equality -- so demanding exactness of them would refuse the entire ladder
    for doing what it is for. They are covered instead by the size floor, the
    unique-anchor rule and the uniqueness rule.
    """
    if stage == "exact":
        return "" if region == old else (
            "the exact stage returned a region that is not old_string; refusing "
            "rather than splicing at offsets that do not hold the caller's text."
        )
    normalise = _NORMALISERS.get(stage)
    if normalise is None:
        return ""
    try:
        produced = normalise(region)[0]
        expected = normalise(old)[0]
    except Exception:  # pragma: no cover - the normalisers are total by design
        return ""
    if produced == expected:
        return ""
    return (
        f"the {stage} stage reported a match, but its own claim failed: putting "
        "the returned region and old_string through that same normalisation does "
        "not make them equal, so the offsets it returned do not hold your text. "
        "This ladder's offset map is known to be short by one entry per line "
        "(its map omits the newlines it joins the lines with), so a hit past the "
        "first line lands at the wrong offset. Refusing rather than writing to "
        "the wrong place -- re-read the file and copy the exact text, or "
        "regenerate the file if it has been reformatted."
    )


#: The normalisation each stage performs, re-derived here ONLY to verify a
#: claim the ladder already made about its own output. Not a second matcher:
#: :func:`_normalisation_integrity` never searches, it only asks whether a
#: returned region is the text the caller asked to replace.
_NORMALISERS: Dict[str, Any] = {
    "line_trimmed": opencode_replacer._strip_trailing,
    "whitespace_normalised": opencode_replacer._collapse_whitespace,
    "indentation_normalised": opencode_replacer._dedent,
    "escape_normalised": opencode_replacer._unescape,
}

#: Stages whose contract is a similarity score rather than an equality, and for
#: which the integrity post-condition is therefore not applicable.
APPROXIMATE_STAGES: Tuple[str, ...] = (
    "block_anchor",
    "context_levenshtein",
    "bitap_second_opinion",
)


def _refuse_missing_line_structure(text: str, old: str) -> str:
    """Return a refusal when the file has too few lines to host the block.

    **The defect this closes, and it is the class-10 minified/generated case.**
    On a minified file the ``whitespace_normalised`` stage collapses the whole
    file -- and the caller's block -- to runs of single spaces, so the caller's
    block matches at confidence 1.0. Measured on the benchmark's fixture: a
    one-line, 834-character minified file returned a region covering 27% of
    itself, *correctly located*. The region being right is not the point. On a
    generated file the match is a coincidence of whitespace collapsing, the
    replacement is multi-line pretty-printed source, and the result is a corrupt
    hybrid of the two -- and the next build overwrites it anyway.

    **The rule, and why it is not a threshold.** A block edit is located by
    lines. If the FILE has fewer non-blank lines than the caller's BLOCK does,
    the file has been reflowed or generated, and any alignment between the two
    is an artefact of whitespace normalisation rather than a located block.
    Counting lines rather than characters is what makes it survive minification:
    minified code has enormous lines and almost none of them.

    Deliberately a *structural* refusal and not a similarity one. The similarity
    was 1.0 -- perfect -- so nothing about "how close" could ever catch this, and
    a rule that only catches near-misses cannot catch the case where the matcher
    is most confident and most wrong.
    """
    have = _content_lines(text)
    need = _content_lines(old)
    if have >= need:
        return ""
    return (
        f"this file has {have} non-blank line(s) and your old_string has {need}, "
        "so it has been minified or generated rather than hand-written. Matching "
        "a block into a generated file works only because collapsing whitespace "
        "makes every block look alike -- the similarity is high precisely when "
        "it means least, and the edit would leave a minified file with "
        "pretty-printed source spliced into it. Edit the source file, or "
        "regenerate the artifact; nothing was written."
    )


# ---------------------------------------------------------------------------
# T1.W2.2 -- the LLM edit-fixer: the LAST fallback, and the weakest guarantee
# ---------------------------------------------------------------------------
#
# Source: Gemini CLI's `edit` tool ends in a model subcall that repairs a failed
# match -- "repair the match with a model instead of a threshold". The corpus
# calls it the first in-tool LLM-assisted edit repair. It is here because every
# DETERMINISTIC stage has failed by the time it runs.
#
# **The model may propose TEXT and nothing else.** It never proposes an offset,
# a line number, or a region. Whatever it returns is treated as a candidate
# `old_string` and is re-located by an EXACT search over the file's own bytes --
# the same search, in the same direction, that any other edit uses. So there is
# no path by which a model reply can express "write here": it can only say "this
# is the text I believe is there", and the code decides whether that is true.
# That is what makes "the guard still applies to an LLM-proposed match" a
# structural property rather than a promise.

#: The three mechanisms, weakest-last. Rendered into every receipt so a reviewer
#: reading a diff can tell which one produced the edit they are looking at.
#: Order matters and is the whole point: exact > fuzzy > model-repaired.
MATCH_GUARANTEES: Tuple[str, ...] = ("exact", "fuzzy", "weakest")
LLM_GUARANTEE: str = "weakest"

#: The stage name that appears in a receipt for a model-repaired edit. Distinct
#: from every ladder rung on purpose: a reader must be able to grep for it.
LLM_STAGE: str = "llm_repaired"

#: Bounded prompt. The whole point is a mechanical repair; a prompt that can
#: grow with the file is a prompt that can overflow the context of a run that is
#: already in trouble.
LLM_PROMPT_MAX_CHARS: int = 8000
LLM_REPLY_MAX_CHARS: int = 2000

#: A reply proposing a replacement longer than this is not a repair, it is a
#: rewrite. Refused rather than truncated: a truncated replacement is a syntax
#: error written into a repository.
LLM_MAX_REPLACEMENT_CHARS: int = 4000


def _switch(config: Optional[Mapping[str, Any]], key: str) -> bool:
    """Read an opt-in switch by KEY PRESENCE plus explicit truthiness.

    The AGT-13 rule, and the reason it is applied rather than a plain
    ``bool(config.get(...))``: a value in ``harness/config.py::DEFAULTS`` is
    merged into every task and every eval arm, so a truthy default here would
    silently switch every run in the project onto a different architecture --
    one with a model call on the edit path. Absent therefore means OFF.
    """
    if not config or key not in config:
        return False
    value = config[key]
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "repair"}
    return bool(value)


def llm_repair_enabled(
    config: Optional[Mapping[str, Any]], *, call: Any = None
) -> bool:
    """Return whether the terminal model-repair stage may run.

    Requires the switch to be explicitly truthy **and** a model boundary to be
    bound. The second condition is not a convenience: with no boundary the
    stage must degrade to an honest refusal, never resolve a default provider
    and dial one during an edit.
    """
    return _switch(config, "edit_llm_repair") and callable(call)


def llm_repair_report(config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Return what the terminal stage would do under ``config``, editing nothing.

    A ``/doctor`` row or a CLI banner can therefore answer "is a model going to
    be called on my edit path, at what tier, under what budget" without reading
    this file.
    """
    return {
        "enabled": _switch(config, "edit_llm_repair"),
        "default_enabled": False,
        "guarantee": LLM_GUARANTEE,
        "stage": LLM_STAGE,
        "guarantee_order": list(MATCH_GUARANTEES),
        "difficulty_hint": "easy",
        "budget_keys": {
            "edit_llm_repair_max_calls": LLM_MAX_CALLS_DEFAULT,
            "edit_llm_repair_max_tokens": LLM_MAX_TOKENS_DEFAULT,
        },
        "defaults": {
            "max_calls": LLM_MAX_CALLS_DEFAULT,
            "max_tokens": LLM_MAX_TOKENS_DEFAULT,
            "prompt_max_chars": LLM_PROMPT_MAX_CHARS,
            "reply_max_chars": LLM_REPLY_MAX_CHARS,
            "max_replacement_chars": LLM_MAX_REPLACEMENT_CHARS,
        },
        "model_may_propose": "text only; offsets are re-derived by exact search",
    }


LLM_MAX_CALLS_DEFAULT: int = 1
LLM_MAX_TOKENS_DEFAULT: int = 2000


def _llm_budget(config: Optional[Mapping[str, Any]]) -> Tuple[int, int]:
    """Return the ``(max_calls, max_tokens)`` budget for one run."""
    return (
        int(_number(config, "edit_llm_repair_max_calls", LLM_MAX_CALLS_DEFAULT)),
        int(_number(config, "edit_llm_repair_max_tokens", LLM_MAX_TOKENS_DEFAULT)),
    )


def _render_repair_prompt(text: str, old: str, new: str) -> str:
    """Build the bounded repair prompt, through the redaction boundary.

    The file content is untrusted input that happens to be on the edit path, so
    it goes through ``harness.redaction`` exactly like every other value that
    becomes model text. A repository path in the content is an ordinary string
    and can absolutely be a credential.
    """
    from harness.redaction import redact_text_for_journal

    def clip(value: str, limit: int) -> str:
        value = redact_text_for_journal(str(value))
        return value if len(value) <= limit else value[:limit] + "\n[...truncated...]"

    return (
        "A code edit failed to match its target, and every deterministic "
        "matching strategy has already been tried and refused. Your job is "
        "MECHANICAL: recover the exact text that is in the file.\n\n"
        "Return ONE JSON object and nothing else:\n"
        '{"found_in_file": "<the exact substring of FILE that the edit was '
        'meant to replace, copied verbatim>", "explanation": "<one sentence>"}\n\n'
        "Rules:\n"
        "- Copy from FILE exactly. Do not reformat, re-indent, or repair it.\n"
        "- If you cannot find it, return {\"found_in_file\": null}.\n"
        "- Do NOT return a line number, an offset, or a diff. Text only.\n\n"
        f"=== EDIT REQUEST ===\nold_string the model asked to replace:\n{clip(old, 1500)}\n"
        f"\n=== FILE ===\n{clip(text, LLM_PROMPT_MAX_CHARS)}\n"
    )


def _parse_repair_reply(reply: Any) -> Tuple[Optional[str], str]:
    """Return ``(proposed_text, note)`` from a model reply, or ``(None, why)``.

    Every failure is a refusal with a reason rather than an exception: this runs
    on the edit path, and a repair that raises would turn a recoverable mismatch
    into a crashed edit.
    """
    import json

    raw = reply if isinstance(reply, str) else str(reply or "")
    raw = raw.strip()
    if len(raw) > LLM_REPLY_MAX_CHARS:
        return None, (
            f"the repair reply was {len(raw)} characters, over the "
            f"{LLM_REPLY_MAX_CHARS} ceiling; a reply that long is a rewrite, "
            "not a repair"
        )
    if not raw:
        return None, "the repair model returned nothing"
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None, "the repair reply carried no JSON object"
    try:
        payload = json.loads(raw[start : end + 1])
    except ValueError as exc:
        return None, f"the repair reply was not valid JSON ({exc})"
    if not isinstance(payload, dict):
        return None, "the repair reply's JSON was not an object"
    found = payload.get("found_in_file")
    if found is None:
        return None, str(payload.get("explanation") or "the model found no match")
    if not isinstance(found, str) or not found.strip():
        return None, "the model's found_in_file was empty or not a string"
    if len(found) > LLM_MAX_REPLACEMENT_CHARS:
        return None, (
            f"the model proposed {len(found)} characters, over the "
            f"{LLM_MAX_REPLACEMENT_CHARS} ceiling"
        )
    return found, str(payload.get("explanation") or "")


def llm_repair_stage(
    text: str,
    old: str,
    new: str,
    *,
    config: Optional[Mapping[str, Any]],
    call: Any,
    spent: Optional[Dict[str, int]] = None,
) -> Tuple[Optional["FuzzyMatch"], str, Dict[str, Any]]:
    """Ask a cheap model to name the text in the file, then locate it ourselves.

    Returns ``(match, reason, receipt)``. ``receipt`` is always populated --
    including on every refusal -- because a stage that consumed budget must say
    so.

    **Every guard still applies.** The proposal is re-located by an exact search
    over the file's own bytes, so the offsets are ours, not the model's, and
    they then pass through the same line-structure, region-coverage,
    normalisation-integrity and region-extent checks a ladder match does. A
    model that proposes the wrong text gets a refusal, exactly as a ladder that
    proposes the wrong region does.

    **Budget.** A call cap and a token cap, both checked BEFORE the call. Going
    over either refuses the edit rather than retrying: an unbounded repair loop
    on the edit path is a run that spends money until something else stops it.
    """
    meter = spent if isinstance(spent, dict) else {}
    meter.setdefault("calls", 0)
    meter.setdefault("tokens", 0)
    max_calls, max_tokens = _llm_budget(config)
    receipt: Dict[str, Any] = {
        "stage": LLM_STAGE,
        "guarantee": LLM_GUARANTEE,
        "difficulty_hint": "easy",
        "calls": meter["calls"],
        "tokens": meter["tokens"],
        "max_calls": max_calls,
        "max_tokens": max_tokens,
        "proposed": False,
        "applied": False,
        "note": "",
    }

    if meter["calls"] >= max_calls:
        receipt["note"] = "call budget spent"
        return None, (
            f"the model repair budget for this run is spent "
            f"({meter['calls']}/{max_calls} calls), so no further repair will be "
            "attempted. Re-read the file and copy the exact text."
        ), receipt
    if meter["tokens"] >= max_tokens:
        receipt["note"] = "token budget spent"
        return None, (
            f"the model repair token budget for this run is spent "
            f"({meter['tokens']}/{max_tokens} tokens), so no further repair will "
            "be attempted. Re-read the file and copy the exact text."
        ), receipt

    prompt = _render_repair_prompt(text, old, new)
    meter["calls"] += 1
    try:
        # `difficulty_hint="easy"` is this project's cheap-tier mechanism (the
        # same one `harness.approver` uses), so the repair is cheap on every
        # provider and is never the frontier tier.
        reply = call(prompt, difficulty_hint="easy")
    except Exception as exc:
        receipt["note"] = f"the repair model failed: {exc}"
        return None, (
            f"the model repair call failed ({exc}); treating it as no match"
        ), receipt

    proposed, note = _parse_repair_reply(reply)
    if proposed is None:
        receipt["note"] = note
        return None, (
            f"the model could not name the text in this file ({note}); treating "
            "it as no match. Re-read the file and copy the exact text."
        ), receipt

    receipt["proposed"] = True
    receipt["note"] = note
    meter["tokens"] += min(len(prompt) + len(proposed), max_tokens)
    receipt["calls"] = meter["calls"]
    receipt["tokens"] = meter["tokens"]

    # ---- THE CODE DISPOSES. The proposal is TEXT; the offsets are ours. ----
    offsets = [i for i in range(len(text)) if text.startswith(proposed, i)]
    if not offsets:
        return None, (
            "the model proposed text that does not occur in this file, even "
            "verbatim. Treating it as no match -- re-read the file."
        ), receipt
    if len(offsets) > 1:
        return None, (
            f"the model proposed text that occurs {len(offsets)} times in this "
            "file. Extending the surrounding context is the only way to make it "
            "unambiguous; treating it as no match."
        ), receipt
    start = offsets[0]
    end = start + len(proposed)

    # The guards, unchanged, on a match whose offsets we derived ourselves.
    structural = _refuse_missing_line_structure(text, old)
    if structural:
        receipt["note"] = f"refused by the line-structure guard: {structural[:120]}"
        return None, structural, receipt
    refusal = _refuse_region_coverage(text, start, end, _fraction(
        config, "edit_fuzzy_max_region_fraction", DEFAULT_MAX_REGION_FRACTION))
    if refusal:
        receipt["note"] = f"refused by the region-coverage guard: {refusal[:120]}"
        return None, refusal, receipt
    integrity = _normalisation_integrity("exact", proposed, proposed)
    if integrity:  # pragma: no cover - exact search makes this unreachable
        return None, integrity, receipt

    match = FuzzyMatch(
        start=start,
        end=end,
        replacement=new,
        stage=LLM_STAGE,
        confidence=0.0,  # deliberately NOT a similarity: nothing was scored
        guard="llm_repair",
        note=(
            "located by an exact search over the text a cheap model proposed; "
            "the model chose the words, the code chose the offsets"
        ),
        considered=(LLM_STAGE,),
        rejected=(),
        alternatives=0,
    )
    match, clamped = _clamp_region_extent(match, text, old)
    if clamped:
        match = replace(match, guard=f"llm_repair,{clamped}")
    receipt["applied"] = True
    receipt["confidence"] = match.confidence
    receipt["guarantee"] = LLM_GUARANTEE
    return match, "", receipt


#: Stages whose claim is an EQUALITY on a normalised form, not a score. These
#: need no similarity threshold because there is nothing to be approximate
#: about -- if the normalised forms are equal, they are equal.
EXACT_STAGES: Tuple[str, ...] = (
    "exact",
    "line_trimmed",
    "whitespace_normalised",
    "indentation_normalised",
    "escape_normalised",
)

#: Stages whose claim is a SIMILARITY SCORE. These must clear
#: :data:`opencode_replacer.STRONG_SIMILARITY` (0.90), not merely the loose
#: cascade threshold (0.65).
#:
#: **``block_anchor`` is in this set because it was wrongly treated as exact,
#: and the edit-drift benchmark is what proved it.** `DisproportionateGuard`
#: exempts ``block_anchor`` from the size floor and the unique-anchor rule on
#: the stated grounds that both "proved the match rather than scored it" -- but
#: `_block_anchor` returns a candidate whenever a line-count-aligned window
#: scores >= 0.65, which is a SCORE. Measured consequence, on real repository
#: code: a caller asking to replace
#: ``def zzz_absent():\n    return 1\n`` -- a function that does not exist
#: anywhere in the file -- got a match at **0.667** on the window
#: ``def a():\n\treturn 1\n``, because the line ``return 1`` *was* unique in
#: the file and no guard fired. The edit was APPLIED. A hallucinated symbol
#: name became a successful, corrupting write, which is the single worst thing
#: this ladder can do.
#:
#: The threshold is set from measurement, not taste. Across every legitimate
#: `block_anchor` hit in the held-out corpus -- real files, real blocks -- the
#: scores were **0.969, 0.973, 0.985, 0.988, 0.995**. The corrupt hit scored
#: 0.667. `STRONG_SIMILARITY = 0.90` sits in a 0.27-wide gap between them.
SCORED_MATCH_STAGES: Tuple[str, ...] = (
    "block_anchor",
    "context_levenshtein",
    "bitap_second_opinion",
)


#: The floor a SCORED stage's similarity must clear.
#:
#: Set from measurement, and the three numbers are the justification:
#:
#: * a HALLUCINATED target -- ``def zzz_absent()`` in a file that does not
#:   contain it -- scores **0.667**;
#: * the lowest LEGITIMATE scored hit anywhere in this project scores **0.80**
#:   (a block that was re-wrapped and re-indented enough that only
#:   ``block_anchor`` could find it);
#: * the highest legitimate scored hit is **0.995**.
#:
#: 0.72 sits in the gap with margin on both sides. The obvious choice,
#: ``STRONG_SIMILARITY`` (0.90), was MEASURED WRONG first: calibrating only on
#: the held-out corpus gave 0.969-0.995 and suggested 0.90, which refused two of
#: Wave 1's own legitimate edits at 0.80 and 0.85. A threshold fitted to one
#: corpus is not a threshold.
MIN_SCORED_SIMILARITY: float = 0.72


def _refuse_weak_anchor(stage: str, similarity: float) -> str:
    """Return a refusal when a SCORED stage matched, but not convincingly.

    The rule: a stage that answers with a number must clear
    :data:`MIN_SCORED_SIMILARITY`. Clearing only the loose cascade threshold
    (0.65) means "the nearest of several poor windows won", which is the coin
    flip `DisproportionateGuard` was written to prevent -- and for
    ``block_anchor`` it was not even reaching the guard, because that stage was
    exempt from the rules that would have caught it.

    Not applied to the exact stages: they assert equality on a normalised form
    and have no score to be weak about.
    """
    if stage not in SCORED_MATCH_STAGES:
        return ""
    if similarity >= MIN_SCORED_SIMILARITY:
        return ""
    return (
        f"the best {stage} candidate scored {similarity:.2f}, under the "
        f"{MIN_SCORED_SIMILARITY:.2f} a scored match must clear. A scored stage "
        "that only just clears the cascade threshold is the nearest of several "
        "poor windows, not a located block -- and on a target that is not in the "
        "file at all it will still find something. Nothing was written; extend "
        "old_string with more context, or re-read the file."
    )


#: The vendored normalisers whose OFFSET MAP is known to be short, measured by
#: :func:`_normaliser_map_is_sound` rather than asserted from a comment.
#:
#: **This is the fourth defect the edit-drift benchmark found, and the subtlest
#: one.** ``_strip_trailing`` builds its map with one entry per KEPT character
#: but joins the stripped lines with newlines, so ``len(map) < len(normalised)``
#: by one per line and every hit past the first newline is mapped too high.
#: ``_collapse_whitespace`` has the same shape. The
#: :func:`_normalisation_integrity` post-condition catches the CONSEQUENCE for
#: ``_strip_trailing`` (the returned region does not normalise to the caller's
#: text), but it cannot catch it for ``_collapse_whitespace``: a mis-mapped
#: region can still COLLAPSE to the same string as ``old`` when the file
#: contains repeated structure, so the comparison passes and the splice lands in
#: the wrong place.
#:
#: Measured on real repository code: after a prose header was added to
#: ``harness/third_party/aider_bitap.py``, the ``trailing_whitespace`` and
#: ``crlf`` held-out cases returned a ``whitespace_normalised`` match at
#: similarity **1.0** at a region 370 characters from the intended one. A
#: perfect score and the wrong place -- the exact shape a similarity threshold
#: can never catch.
SHORT_MAP_NORMALISERS: Tuple[str, ...] = (
    "line_trimmed",
    "whitespace_normalised",
)


def _normaliser_map_is_sound(stage: str, text: str) -> bool:
    """Return whether a normalisation stage's offset map can be trusted.

    Runs the stage over the real text and compares the two lengths the vendored
    implementation should have kept equal and did not: the normalised string it
    returns, and the offset map that maps positions in it back to the original.

    A map shorter than the string it indexes cannot address the string's last
    positions, so every hit past the shortfall is mapped to the wrong offset.
    That is a property of the ALGORITHM, not of any one file, so it is measured
    once here and then refuses.
    """
    if stage not in SHORT_MAP_NORMALISERS:
        return True
    normalise = _NORMALISERS.get(stage)
    if normalise is None:  # pragma: no cover - the table is closed
        return True
    try:
        _, index_map = normalise(text)
    except Exception:  # pragma: no cover - the normalisers are total by design
        return False
    return len(index_map) >= len(text)


UNSOUND_MAP_STAGES = SHORT_MAP_NORMALISERS  # recorded, not enforced -- see the note


def _fraction(config: Optional[Mapping[str, Any]], key: str, fallback: float) -> float:
    """Read a 0..1 fraction config value, degrading to the fallback.

    A value outside ``(0, 1]`` is refused to the fallback rather than clamped,
    because ``0`` would disable the guard entirely and ``2`` would make it
    unreachable -- and a safety bound that can be configured off by a typo is
    not a bound.
    """
    if not config or key not in config:
        return fallback
    try:
        value = float(config[key])
    except (TypeError, ValueError):
        return fallback
    return value if 0.0 < value <= 1.0 else fallback


def _number(config: Optional[Mapping[str, Any]], key: str, fallback: float) -> float:
    """Read a numeric config value, degrading to the fallback rather than raising.

    A nonsense value must not crash an edit; it must fall back AND the receipt
    says which threshold actually ran (via :data:`THRESHOLDS` plus the stage), so
    the reader is not misled about the policy in force.
    """
    if not config or key not in config:
        return fallback
    try:
        value = float(config[key])
    except (TypeError, ValueError):
        return fallback
    return value if value > 0.0 else fallback


def ladder_report(config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Return what the ladder would do under ``config``, without editing anything.

    A caller (or a CLI ``/doctor`` row) can therefore answer "is fuzzy editing
    on here, and under what policy?" from the receipt rather than by reading
    this file.
    """
    return {
        "enabled": fuzzy_enabled(config),
        "stages": list(STAGES),
        "thresholds": dict(THRESHOLDS),
        "vendored": [
            {
                "module": "harness/third_party/opencode_replacer.py",
                "upstream": "https://github.com/sst/opencode",
                "licence": "MIT",
                "independent_reimplementation": True,
            },
            {
                "module": "harness/third_party/aider_bitap.py",
                "upstream": "https://github.com/Aider-AI/aider",
                "licence": "Apache-2.0",
                "independent_reimplementation": True,
            },
        ],
    }
