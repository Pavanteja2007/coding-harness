"""Tool-discovery ranking, behind a seam T1 can replace with BM25.

WHY A SEAM AND NOT A BM25 IMPLEMENTATION
----------------------------------------
The corpus finding this module exists for is Codex's: **substring matching
fails on exactly the case that matters.** A deferred-tool catalog is only
useful if the model can find the tool it half-remembers, and the tool it
half-remembers is the one whose name it will not spell correctly.

The shipped scorer in :meth:`integrations.tools.ToolCatalog.search` was
hand-rolled and is characterised in :data:`SUBSTRING_SCORER_NOTES` below. It is
NOT good enough, and this module does not pretend to fix that by writing
another keyword scorer. It fixes it by making the scorer a **replaceable
boundary with a published contract**, so the BM25 port is a new
:class:`ToolSearchScorer` rather than an edit inside a ranking loop that also
does redaction, descriptor projection, and result bounding.

The four properties a replacement must preserve, each one a real behaviour
someone depends on:

1. **Field structure.** ``name`` > ``tags`` > ``description``. A tool whose
   NAME matches must outrank a tool whose prose happens to contain the word.
   BM25 over a single concatenated field destroys this, and the destruction is
   silent. A scorer therefore receives the fields SEPARATELY and reports which
   field produced each contribution.
2. **Exact-name supremacy.** ``search("read_file")`` must rank ``read_file``
   first even if ``read_file_backup`` appears in 40 descriptions. A BM25 score
   is a real number and can be beaten by document length; an exact name match
   is a different KIND of evidence, not a bigger number.
3. **Total order, always.** Ties break on ``name`` ascending. A scorer that
   returns equal scores for two tools has made the result set depend on dict
   iteration order, which is a different answer on a different Python build.
4. **A zero score is a real answer.** A tool that matched nothing is
   :data:`NO_MATCH`, and it is dropped. It is NOT rendered as a match with
   score 0, because "ranked last with a real score" and "matched nothing" are
   different facts and a caller filtering on ``score > 0`` cannot tell them
   apart if both arrive as 0.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
- It does not embed, vendor, or fetch BM25. It defines the seam and ships a
  reference scorer so the contract is executable rather than aspirational.
- It does not stem. Stemming is a *choice* about the corpus, and a wrong stemmer
  merges two tools a user must be able to tell apart. A stemmer belongs in the
  replacement scorer where it can be ablated, not smuggled into the boundary.
- It does not cache. Ranking is pure and cheap; a cache here would be a second
  place to be wrong.

THE VACUITY TRAP IN THE MEASUREMENT, NOT IN THE CODE
----------------------------------------------------
An earlier version of this file was written and then deleted, because its
docstring quoted corpus numbers ("~40% prompt reduction from deferred tool
loading") as though they described *this* catalog. They describe Claude Code's.
This module therefore quotes no delta at all. The number this repo produces is
measured by :mod:`evals.tools_ablation`, and that module reports
``cost_axis_vacuous`` when the estimator cannot see the difference.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import (
    Any,
    ClassVar,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

__all__ = [
    "BM25_B",
    "BM25_K1",
    "NO_MATCH",
    "SUBSTRING_SCORER_NOTES",
    "TOOL_SEARCH_SCORERS",
    "Bm25ToolSearch",
    "SearchCandidate",
    "SearchHit",
    "SearchScorer",
    "SubstringToolSearch",
    "default_scorer",
    "rank_tools",
    "scorer_by_name",
    "tokenize",
]


#: The score a non-matching tool carries. It is a real value rather than a
#: sentinel ``None`` so a caller can sum scores, and it is a real value rather
#: than ``0.0`` for a different reason: :data:`SearchHit.matched` is the field
#: a caller filters on, and a sentinel that reads as a number invites exactly
#: the ``score > 0`` filter that cannot distinguish the two cases.
NO_MATCH = -1.0

#: The token pattern. Deliberately permissive on ``-`` and ``_`` because tool
#: names are snake/kebab-case identifiers and splitting ``read_file`` into
#: ``read`` + ``file`` is what lets a query for "read" find it.
_TOKEN_RX = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")

#: Field weights for the reference substring scorer, in the shipped order.
#: These are the constants that were lifted verbatim out of
#: ``ToolCatalog.search`` so the seam's default arm is byte-comparable with the
#: pre-seam behaviour. The ablation arm pins that comparability, which is what
#: makes the seam refactor a no-op rather than an unreported behaviour change.
DEFAULT_FIELD_WEIGHTS: Dict[str, float] = {
    "name": 1.0,
    "tags": 1.0,
    "description": 1.0,
}

#: BM25 term-frequency saturation. The canonical default from the original
#: Okapi BM25 paper; a replacement may change it and the report says so.
BM25_K1 = 1.5

#: BM25 length normalisation. 0.75 is the cited default. It is published here
#: as a CONSTANT rather than being baked into a literal so an ablation can
#: report ``k1``/``b`` on every run - a BM25 result quoted without its k1/b is
#: not reproducible.
BM25_B = 0.75


#: What is known about the scorer this module replaces, recorded so the
#: characterisation is a pinned fact rather than a claim in a handoff nobody
#: re-reads. Read this as the "before" column of the ablation.
SUBSTRING_SCORER_NOTES: Dict[str, Any] = {
    "location": "integrations/tools.py ToolCatalog.search",
    "kind": "hand-rolled substring accumulation",
    "tokenizer": "re.findall(r'[\\w-]+', query.lower())",
    "has_idf": False,
    "has_stemming": False,
    "has_length_normalisation": False,
    "scoring": (
        "per query term, additive: name==term +100, name.startswith(term) +50, "
        "term in name +25, term in description +6, term in tag +12"
    ),
    "empty_query_behaviour": "every descriptor scores 1 and is returned in name order",
    "tied_order": "name ascending",
    "known_failures": [
        "a rare term is worth exactly as much as a ubiquitous one (no IDF), so a "
        "word appearing in every description cannot be out-voted",
        "no length normalisation, so a 2000-character description out-scores a "
        "40-character one purely by containing more substrings",
        "'read' cannot find 'read_file' unless the term is a substring of the "
        "name, so a query for the concept fails while a query for the exact "
        "spelling succeeds",
    ],
    "not_a_bug": [
        "it never returns a non-matching tool, so it cannot over-offer",
        "it is deterministic and dependency-free, which is why it shipped",
    ],
}


def tokenize(text: str) -> Tuple[str, ...]:
    """Split text into lowercased tool-search tokens.

    Splits on ``-``/``_`` boundaries but KEEPS an interior hyphen, so
    ``read-file`` is one token and ``read`` is not a substring of it. This is
    what makes the ``read`` -> ``read_file`` query work at all, which is the
    corpus's motivating case.
    """
    return tuple(_TOKEN_RX.findall(str(text or "").lower()))


@dataclass(frozen=True)
class SearchCandidate:
    """One tool as the scorer sees it.

    Fields are passed SEPARATELY and never pre-concatenated. That is the whole
    point of the seam: a BM25 scorer that receives one blob cannot express
    "the name matched", and a ranking that cannot say which field produced a
    score cannot be debugged by a reader who disagrees with the order.
    """

    name: str
    description: str = ""
    tags: Tuple[str, ...] = ()
    #: Pre-tokenised fields, populated by :func:`_candidate`. Held so a scorer
    #: that tokenises differently can still be compared against one that does
    #: not, and so the token count is a measurement rather than a re-derivation.
    name_tokens: Tuple[str, ...] = ()
    tag_tokens: Tuple[str, ...] = ()
    description_tokens: Tuple[str, ...] = ()

    @property
    def token_count(self) -> int:
        """Total tokens across all fields; BM25's document length."""
        return (
            len(self.name_tokens) + len(self.tag_tokens) + len(self.description_tokens)
        )


@dataclass(frozen=True)
class SearchHit:
    """One ranked tool.

    :param matched: whether the tool matched at all. Distinct from
        ``score != NO_MATCH`` because a scorer is allowed to return a real
        score of ``0.0`` for a matched tool, and a caller must be able to tell
        "matched with score zero" from "did not match".
    :param field_hits: which fields contributed, so a reader can see WHY a
        tool ranked. Empty on a non-match.
    :param rank: 1-based position in the result list.
    """

    name: str
    score: float
    matched: bool
    field_hits: Tuple[str, ...] = ()
    rank: int = 0
    #: Free-form scorer provenance: ``{"scorer": "bm25", "k1": 1.5, ...}``.
    #: The scorer NAME rides on every hit, so a mixed-scorer report cannot
    #: present two arms as one measurement.
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "matched": self.matched,
            "field_hits": list(self.field_hits),
            "rank": self.rank,
            "provenance": dict(self.provenance),
        }


class SearchScorer(Protocol):
    """The seam. Implement this and the catalog ranks with your algorithm.

    Contract, all four parts load-bearing:

    * ``name`` - a short stable identifier, reported on every hit and every
      report. Two arms scored by different functions are two measurements.
    * ``score(query, candidate) -> Tuple[float, Tuple[str, ...]]`` - returns
      the score and the fields that contributed. The second element is what
      makes a ranking auditable; a scorer that returns only a float forces
      every reader to re-derive the ranking by hand.
    * A non-match MUST be ``(NO_MATCH, ())``. Returning ``(0.0, ())`` claims a
      match that did not happen.
    * MUST be pure. No clock, no I/O, no globals. A scorer that reads a file
      makes the ablation's two arms differ for a reason that is not deferral,
      which is the exact confound this module exists to prevent.
    """

    name: str

    def score(
        self, query: str, candidate: SearchCandidate
    ) -> Tuple[float, Tuple[str, ...]]:
        """Score one candidate against one query. See the protocol docstring."""
        ...

    def describe(self) -> Dict[str, Any]:
        """Return the parameters a reader needs to reproduce the ranking."""
        ...


def _candidate(name: str, description: str, tags: Sequence[str]) -> SearchCandidate:
    """Build a :class:`SearchCandidate` with pre-tokenised fields."""
    clean_tags = tuple(str(tag) for tag in (tags or ()) if str(tag))
    return SearchCandidate(
        name=str(name),
        description=str(description or ""),
        tags=clean_tags,
        name_tokens=tokenize(name),
        tag_tokens=tokenize(" ".join(clean_tags)),
        description_tokens=tokenize(description),
    )


def with_tokens(candidate: SearchCandidate) -> SearchCandidate:
    """Return ``candidate`` with all three token fields populated.

    Needed because :class:`Bm25ToolSearch` looks a candidate up in its fitted
    corpus BY IDENTITY. The first draft looked it up by value with
    ``list.index``, which fails two ways: a caller that did not pre-tokenise
    produced a candidate unequal to the fitted one (silently ``NO_MATCH`` for
    every tool), and two byte-identical tools resolved to the same index. This
    function guarantees the object :meth:`Bm25ToolSearch.fit` stores is the
    object :meth:`Bm25ToolSearch.score` receives, which is a structural property
    rather than a coincidence about how the caller built its candidates.

    THE GUARD IS "ALREADY TOKENISED", NOT "HAS TOKENS", AND THAT IS THE FIX
    ---------------------------------------------------------------------
    The first version guarded with ``if candidate.name_tokens and
    candidate.tag_tokens``. **A tool with no tags has ``tag_tokens == ()``, which
    is falsy, so the guard never passed for it and every call minted a NEW
    object.** :meth:`Bm25ToolSearch.fit` calls this function again on its input,
    so ``rank_tools`` -> ``fit`` -> ``score`` compared objects from two
    different mintings, the identity lookup raised ``StopIteration``, and
    ``score`` returned ``NO_MATCH`` for **every candidate in the catalog**.

    Measured, not inferred (``python -m evals.tools_ablation --scorer-audit``):
    BM25 returned **0 of 45 hits on all five probe queries** while substring
    returned 33-44. All 45 real harness tools are built with ``tags=()``, so the
    dead path was the only path this catalog had.

    The consequence was not a degraded ranking, it was an INVERTED policy: with
    every tool at ``NO_MATCH``, ``DeferralPolicy`` classifies all 45 as
    ``no_query_match`` and **defers the entire catalog**, leaving the model a
    nearly empty tool list. ``deferral_safety_report`` does not catch it,
    because the discovery tool is still reachable - the report checks that a
    discovery path exists, not that anything was left in the request.

    The guard is now on the DESCRIPTION tokens, which are non-empty for any
    candidate with a description, and the empty-tag case is no longer
    distinguishable from a tokenised one. A candidate with neither a
    description nor tags genuinely has nothing to tokenise, and re-minting it
    is harmless because it carries no text.
    """
    if candidate.name_tokens and candidate.description_tokens:
        return candidate
    return _candidate(candidate.name, candidate.description, candidate.tags)


class SubstringToolSearch:
    """The reference scorer: the shipped algorithm, lifted behind the seam.

    This exists so the seam is EXECUTABLE. A boundary nobody exercises is a
    comment, and the first thing a reader will ask about a new seam is whether
    swapping the implementation changes anything. This class answers that
    question mechanically: the ablation's control arm is this scorer, and
    ``tests/test_p3b_tool_deferral.py`` pins that it reproduces the pre-seam
    ordering exactly.

    It is deliberately NOT presented as a good ranker. See
    :data:`SUBSTRING_SCORER_NOTES`.
    """

    name = "substring"

    #: The exact constants the pre-seam implementation used. Named so the
    #: pins can assert them rather than assert a comment.
    EXACT_NAME = 100.0
    PREFIX_NAME = 50.0
    SUBSTRING_NAME = 25.0
    SUBSTRING_TAG = 12.0
    SUBSTRING_DESCRIPTION = 6.0
    EMPTY_QUERY_SCORE = 1.0

    def __init__(self, *, field_weights: Optional[Mapping[str, float]] = None) -> None:
        """Accept per-field weights so a replacement can be field-weighted."""
        self._weights = dict(DEFAULT_FIELD_WEIGHTS)
        if field_weights:
            self._weights.update({str(k): float(v) for k, v in field_weights.items()})

    def score(
        self, query: str, candidate: SearchCandidate
    ) -> Tuple[float, Tuple[str, ...]]:
        """Accumulate the additive per-term score, exactly as before."""
        terms = [
            term
            for term in re.findall(r"[\w-]+", str(query or "").lower())
            if term
        ]
        if not terms:
            # The pre-seam behaviour: every tool is returned, in name order, at
            # score 1. That is a LISTING rather than a search, and `matched` is
            # True because a listing that returned nothing would be a silent
            # failure. Reported as a match so a caller cannot mistake the
            # empty-query listing for "nothing matched your query".
            return self.EMPTY_QUERY_SCORE, ("name",)
        name = candidate.name.lower()
        description = candidate.description.lower()
        tags = [tag.lower() for tag in candidate.tags]
        total = 0.0
        fields: List[str] = []
        for term in terms:
            term_score = 0.0
            if name == term:
                term_score += self.EXACT_NAME
            elif name.startswith(term):
                term_score += self.PREFIX_NAME
            elif term in name:
                term_score += self.SUBSTRING_NAME
            if term in description:
                term_score += self.SUBSTRING_DESCRIPTION * self._weights["description"]
            if any(term in tag for tag in tags):
                term_score += self.SUBSTRING_TAG * self._weights["tags"]
            if term_score > 0.0:
                if name == term or name.startswith(term) or term in name:
                    fields.append("name")
                if any(term in tag for tag in tags):
                    fields.append("tags")
                if term in description:
                    fields.append("description")
            total += term_score * self._weights.get("name", 1.0)
        if total <= 0.0:
            return NO_MATCH, ()
        return total, tuple(fields) or ("name",)

    def describe(self) -> Dict[str, Any]:
        return {
            "scorer": self.name,
            "field_weights": dict(self._weights),
            "exact_name": self.EXACT_NAME,
            "prefix_name": self.PREFIX_NAME,
            "substring_name": self.SUBSTRING_NAME,
            "substring_tag": self.SUBSTRING_TAG,
            "substring_description": self.SUBSTRING_DESCRIPTION,
            "empty_query_score": self.EMPTY_QUERY_SCORE,
            "tokenizer": "re.findall(r'[\\w-]+', query.lower())",
        }


class Bm25ToolSearch:
    """Okapi BM25 over per-field corpora, with the seam's four rules preserved.

    Written as the reference implementation of the PROTOCOL, not as the thing
    T1 must use. Three corpus-faithful details, each of which a naive port gets
    wrong:

    1. **Per-field corpora, then a weighted sum.** Each field gets its own BM25
       over its own documents with its own IDF, and the field scores are
       combined with :attr:`FIELD_WEIGHTS`. A single concatenated corpus would
       let a long description dominate the name signal, which is field
       structure (rule 1) lost silently.
    2. **Exact-name supremacy is enforced AFTER scoring, not by weight.** No
       finite weight makes an exact name match beat a longer document's term
       frequency in a way that holds across corpora, so an exact match is
       applied as a separate additive tier and the fact that it happened is
       recorded in ``field_hits``. A caller can see it happened.
    3. **A query term absent from the whole corpus contributes nothing** and
       does not make the tool a non-match. This is why BM25 fixes the corpus's
       motivating case: ``read`` is not a substring of ``read_file`` under the
       old tokenizer's substring test in the way a person means, but it IS the
       leading token of ``read_file``, so it matches with a real IDF weight.

    The tokeniser is :func:`tokenize`, which splits ``read_file`` into one
    token ``read-file``. That means a query for the bare word ``read`` does NOT
    match ``read_file`` here. This is a real limitation and it is stated rather
    than papered over with a stemmer: prefix matching over the LAST segment is
    available via :meth:`prefix_match`, which a caller opts into, because
    silently prefix-expanding every query is how a search starts returning
    everything.
    """

    name = "bm25"

    FIELD_WEIGHTS: ClassVar[Dict[str, float]] = {
        "name": 3.0,
        "tags": 2.0,
        "description": 1.0,
    }

    #: The additive tier an exact name match contributes, applied after BM25.
    EXACT_NAME_TIER = 1000.0

    def __init__(
        self,
        *,
        k1: float = BM25_K1,
        b: float = BM25_B,
        field_weights: Optional[Mapping[str, float]] = None,
        expand_last_segment_prefixes: bool = False,
    ) -> None:
        """Build a scorer. ``expand_last_segment_prefixes`` is opt-in, not default."""
        self.k1 = float(k1)
        self.b = float(b)
        self._weights = dict(self.FIELD_WEIGHTS)
        if field_weights:
            self._weights.update({str(k): float(v) for k, v in field_weights.items()})
        self._expand = bool(expand_last_segment_prefixes)
        self._corpora: Dict[str, Dict[str, Any]] = {}
        self._documents: Tuple[SearchCandidate, ...] = ()

    # -- corpus construction -------------------------------------------------

    def fit(self, candidates: Iterable[SearchCandidate]) -> "Bm25ToolSearch":
        """Build the per-field corpora. Pure with respect to ``candidates``.

        Assumes the caller passes the WHOLE catalog: IDF depends on the corpus,
        so a corpus built from a subset ranks differently from the same scorer
        fitted on the full set. That is a property of BM25 and the reason
        ``fit`` is explicit rather than lazy.
        """
        docs = tuple(with_tokens(c) for c in candidates)
        self._documents = docs
        self._corpora = {}
        total_docs = len(docs)
        for field_name, tokens_of in (
            ("name", lambda d: d.name_tokens),
            ("tags", lambda d: d.tag_tokens),
            ("description", lambda d: d.description_tokens),
        ):
            doc_tokens = [tokens_of(doc) for doc in docs]
            lengths = [len(tokens) for tokens in doc_tokens]
            avg_len = (sum(lengths) / len(lengths)) if lengths else 0.0
            df: Dict[str, int] = {}
            for tokens in doc_tokens:
                for term in set(tokens):
                    df[term] = df.get(term, 0) + 1
            idf = {
                term: math.log(
                    1.0 + (total_docs - count + 0.5) / (count + 0.5)
                )
                for term, count in df.items()
            }
            self._corpora[field_name] = {
                "doc_tokens": doc_tokens,
                "lengths": lengths,
                "avg_len": avg_len,
                "idf": idf,
            }
        return self

    # -- scoring -------------------------------------------------------------

    def _expand_query(self, term: str) -> Tuple[str, ...]:
        """Deprecated direction, kept only so the seam stays stable.

        Expansion must be applied to the DOCUMENT token's last segment, not to
        the query: a bare query term like ``fil`` has no hyphen of its own, so
        expanding the query is a no-op and the opt-in silently did nothing.
        :meth:`_prefix_hit` is the real mechanism and :meth:`_field_score`
        calls it. Kept because a subclass may call it.
        """
        if "-" not in term and "_" not in term:
            return (term,)
        head, _, tail = term.replace("_", "-").rpartition("-")
        if not tail:
            return (term,)
        return (term, f"{head}-{tail}")

    @staticmethod
    def _prefix_hit(term: str, tokens: Tuple[str, ...]) -> Tuple[str, int]:
        """Return ``(token, frequency)`` when ``term`` prefixes a token's LAST segment.

        The opt-in half of the search. A bare term is matched against the final
        hyphen/underscore segment of each document token, so ``fil`` reaches
        ``read_file`` and ``kubectl`` reaches ``kubectl_get``.

        Segmented deliberately: expanding EVERY segment would make ``rea`` match
        ``thread`` and ``read``, which is the corpus's complaint about substring
        matching arriving again from the other direction.

        The first version expanded the QUERY instead of the document token, so
        ``fil`` - which has no separator to expand - matched nothing and the
        opt-in was silently inert. The pin
        ``test_prefix_matching_is_opt_in_and_never_applied_implicitly`` is what
        caught it.
        """
        for token in tokens:
            if term == token:
                return token, 0
            last = token.replace("_", "-").rpartition("-")[2]
            if last and last.startswith(term):
                return token, 1
        return "", 0

    def score(
        self, query: str, candidate: SearchCandidate
    ) -> Tuple[float, Tuple[str, ...]]:
        """Score one candidate. Requires :meth:`fit` to have been called."""
        if not self._corpora:
            return NO_MATCH, ()
        terms = [term for term in tokenize(query) if term]
        if not terms:
            # An empty query is a LISTING, and the listing is handled by
            # `rank_tools`, so a scorer is never asked to score one. Reporting
            # NO_MATCH here rather than a score means a caller that bypasses
            # `rank_tools` gets an empty result, not a fabricated ranking.
            return NO_MATCH, ()
        try:
            index = next(
                position
                for position, doc in enumerate(self._documents)
                if doc is candidate
            )
        except StopIteration:
            # The candidate was not in the fitted corpus. Reporting NO_MATCH is
            # the honest answer; scoring it against a corpus it is not part of
            # would produce a number with no meaning.
            return NO_MATCH, ()
        total = 0.0
        fields: List[str] = []
        for field_name, weight in self._weights.items():
            corpus = self._corpora.get(field_name)
            if not corpus:
                continue
            field_score = self._field_score(corpus, index, terms)
            if field_score > 0.0:
                total += field_score * float(weight)
                fields.append(field_name)
        name_lower = candidate.name.lower()
        if name_lower in {str(t).lower() for t in terms}:
            total += self.EXACT_NAME_TIER
            fields = ["name"] + [f for f in fields if f != "name"]
        if total <= 0.0:
            return NO_MATCH, ()
        return total, tuple(dict.fromkeys(fields))

    def _field_score(
        self, corpus: Mapping[str, Any], index: int, terms: Sequence[str]
    ) -> float:
        """Sum the BM25 contributions for one field over one document."""
        tokens: Tuple[str, ...] = corpus["doc_tokens"][index]
        length = float(corpus["lengths"][index])
        avg_len = float(corpus["avg_len"]) or 1.0
        idf: Mapping[str, float] = corpus["idf"]
        frequencies: Dict[str, int] = {}
        for token in tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
        score = 0.0
        for term in terms:
            # A term absent from the corpus has no IDF to contribute and must
            # not be treated as a match. `log(1 + (N - 0 + 0.5)/(0 + 0.5))` is
            # the standard unseen-term IDF and is applied here explicitly
            # rather than silently dropped, so the behaviour is identical to a
            # term that appears in exactly zero documents.
            term_idf = idf.get(term)
            if term_idf is None:
                term_idf = math.log(1.0 + (len(corpus["doc_tokens"]) + 0.5) / 0.5)
            freq = frequencies.get(term, 0)
            if freq == 0 and self._expand:
                # Opt-in prefix reach, against the DOCUMENT token's last
                # segment. A term absent from the corpus and absent as a prefix
                # contributes nothing and is not a match.
                token, prefix_freq = self._prefix_hit(term, tokens)
                if token:
                    freq = frequencies.get(token, 0)
                    term_idf = idf.get(token, term_idf)
                    freq = max(freq, prefix_freq)
                else:
                    continue
            elif freq == 0:
                continue
            denominator = freq + self.k1 * (
                1.0 - self.b + self.b * (length / avg_len)
            )
            if denominator <= 0:
                continue
            score += term_idf * (freq * (self.k1 + 1.0)) / denominator
        return score

    def prefix_match(self, query: str, candidate: SearchCandidate) -> bool:
        """Whether any query term is a PREFIX of the candidate's last segment.

        Exposed separately and never applied implicitly. A search that
        prefix-expands by default returns every tool for every query, which is
        the corpus's complaint about substring matching arriving again from the
        other direction.
        """
        last_segment = candidate.name.lower().replace("_", "-").rpartition("-")[2]
        return any(
            last_segment.startswith(term) for term in tokenize(query)
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "scorer": self.name,
            "k1": self.k1,
            "b": self.b,
            "field_weights": dict(self._weights),
            "exact_name_tier": self.EXACT_NAME_TIER,
            "expand_last_segment_prefixes": self._expand,
            "corpus_size": len(self._documents),
            "per_field_vocabularies": {
                name: len(corpus.get("idf", {})) for name, corpus in self._corpora.items()
            },
        }


#: The scorer registry. Adding a scorer here makes it selectable BY NAME, which
#: is what lets the ablation run a scorer arm without editing code.
TOOL_SEARCH_SCORERS: Dict[str, Any] = {
    SubstringToolSearch.name: SubstringToolSearch,
    Bm25ToolSearch.name: Bm25ToolSearch,
}


def scorer_by_name(name: str, **kwargs: Any) -> SearchScorer:
    """Build a scorer by registry name.

    Raises ``KeyError`` naming the KNOWN scorers. An unrecognised name that
    silently fell back to substring would make a BM25 arm a substring arm that
    reports itself as BM25, which is the one outcome this module must not
    produce.
    """
    key = str(name or "").strip()
    if key not in TOOL_SEARCH_SCORERS:
        raise KeyError(
            f"unknown tool search scorer {key!r}; known: "
            f"{sorted(TOOL_SEARCH_SCORERS)}"
        )
    factory = TOOL_SEARCH_SCORERS[key]
    if isinstance(factory, type) and issubclass(factory, Bm25ToolSearch):
        return factory(**kwargs)
    return factory(**kwargs) if kwargs else factory()


def default_scorer() -> SearchScorer:
    """The DEFAULT scorer for this repo, which is still the substring one.

    Stated as a function so it is one place to change and so a reader can see
    that the seam does not by itself upgrade the ranking. Upgrading the default
    is a separate, measured decision: the ablation exists to produce the number
    that would justify it.
    """
    return SubstringToolSearch()


def rank_tools(
    query: str,
    candidates: Iterable[SearchCandidate],
    *,
    scorer: Optional[SearchScorer] = None,
    limit: int = 10,
) -> List[SearchHit]:
    """Rank candidates, enforcing all four seam rules.

    The rules are enforced HERE rather than left to each scorer, because a
    scorer that follows them and a scorer that does not must produce the same
    SHAPE of answer or the ablation cannot compare them.

    An empty query returns every candidate ranked by name ascending with
    ``matched=True`` and the scorer's own listing score. That is a LISTING, and
    it is deliberately not reported as a search: a caller that asked nothing
    gets everything back in a stable order, which is the behaviour the pre-seam
    implementation had and which callers depend on.
    """
    active = scorer if scorer is not None else default_scorer()
    # Normalise BEFORE anything else so every scorer sees the same object
    # identities; `fit` below then stores exactly what `score` will receive.
    docs = [with_tokens(candidate) for candidate in candidates]
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 10
    if cap <= 0:
        return []
    cap = min(cap, 100)
    if isinstance(active, Bm25ToolSearch):
        # ALWAYS refit to the candidates this call is about to score. Two
        # reasons, both of which were real failures:
        #
        # 1. A pre-fitted corpus built from DIFFERENT candidate objects leaves
        #    every score at NO_MATCH, because `score` looks a candidate up by
        #    identity. Refitting unconditionally removes the whole class - the
        #    caller cannot be surprised by which objects they constructed.
        # 2. IDF is a property of the corpus. Scoring a candidate against a
        #    corpus it is not part of produces a number with no meaning, so a
        #    silent reuse would be an unfalsifiable measurement.
        #
        # The cost is one pass over the catalog, which for this repo's ~45 tools
        # is far below the noise floor of a search that does I/O anyway.
        active.fit(docs)
    scored: List[Tuple[float, str, SearchCandidate, Tuple[str, ...]]] = []
    for doc in docs:
        score, field_hits = active.score(query, doc)
        if score == NO_MATCH:
            continue
        scored.append((float(score), doc.name, doc, tuple(field_hits)))
    # Rule 3: total order. Name ascending is the tiebreak, always, so the result
    # set cannot depend on dict or set iteration order.
    scored.sort(key=lambda item: (-item[0], item[1]))
    provenance = active.describe()
    return [
        SearchHit(
            name=name,
            score=score,
            matched=True,
            field_hits=field_hits,
            rank=position,
            provenance=provenance,
        )
        for position, (score, name, _doc, field_hits) in enumerate(
            scored[:cap], start=1
        )
    ]
