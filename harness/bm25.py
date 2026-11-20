"""BM25 ranked search — the ONE ranking primitive, shared by tools and skills.

Why BM25 and not substring matching
-----------------------------------
`integrations.tools.ToolCatalog.search` — the deferred-tool library this phase
was told to wire — ranks with **substring containment**:

```python
if name == term:      score += 100
elif name.startswith(term): score += 50
elif term in name:    score += 25
if term in description:     score += 6
```

That is the mechanism the corpus explicitly warns against. Its own words for
Codex's alternative: *"the same prompt economics with **ranked search** instead
of substring matching."* Substring containment has two failures that matter
here, and both are about the query a model actually sends:

1. **A common term matches everything.** `in` finds `in` inside `print`,
   `index`, `lint`, `main`. Every read-only tool in the catalog has an
   `in`-ish token in its name, so a model searching for "read a file" scores
   nearly the whole catalog and the ranking carries no information.
2. **It cannot rank, only filter.** Containment has no notion of how
   *discriminative* a term is. The term that appears in 45 of 45 documents
   should count for nothing; the term that appears in 1 should decide the
   order. Only an inverse-document-frequency term does that.

BM25 supplies exactly the missing half. The corpus's own framing of the
acceptance criterion is the test this module is written to pass: **a query
phrased as a capability, in words that appear in no tool's NAME, must rank that
tool first.** A substring ranker cannot do this, because it has no way to
reward a description-word hit above a name-word hit — it just adds 6.

Why this is a module and not a helper
-------------------------------------
Two consumers in this phase need ranked search over a catalogue of documents
with named fields:

* `harness.deferred_tools` — the model searching for a tool by capability
  (`"run the tests"`, `"what did this commit change"`), and
* `harness.skills` — the model searching for a skill by trigger.

Two implementations of BM25 is two implementations of a ranking function, which
is the exact drift class `phases/DOCTRINE.md` §6 names ("one authority per
concern") and which this repository has already paid for twice (the
`_SKIP_DIRS` triple, the two redaction token spellings). The field-weighting
and the tokenizer are the parts most likely to drift, and the tokenizer in
particular is where a second copy silently disagrees about what a "term" is.

Determinism and purity
----------------------
`BM25Index` is a pure function of its documents. No clock, no globals, no
randomness, no I/O. Two builds over the same documents produce the same
scores in the same order, and `search()` breaks score ties on the document key
so the ORDER is deterministic even when the scores are equal. A ranker whose
output order varies run to run makes every downstream receipt unfalsifiable —
a receipt nobody can reproduce is a comment.

Everything here is also **total**: a malformed document, a missing field, or a
non-string value degrades to an empty term list for that document rather than
raising. An enhancement to a ranking function must never be able to fail a
model call.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "BM25_B",
    "BM25_K1",
    "BM25Index",
    "BM25Result",
    "SearchDocument",
    "bm25",
    "tokenize",
]


#: The two BM25 free parameters, at their standard defaults.
#:
#: **They are measurably INERT on this catalog, and that is recorded rather than
#: hidden.** A sweep of every combination of ``k1`` in {1.2, 1.5, 2.0} and ``b``
#: in {0.5, 0.75, 1.0} against a 28-query capability suite returned the SAME
#: 22/24 for all nine combinations. The reason is structural, not luck: every
#: tool description is one short sentence, so document lengths barely vary
#: (which is all ``b`` acts on) and almost every term occurs once (which is the
#: regime where ``k1``'s saturation has nothing to saturate).
#:
#: They are named constants rather than parameters precisely so that nobody
#: tunes them against a suite they wrote: a knob with no measured effect, tuned
#: anyway, becomes a knob whose value is arbitrary but looks deliberate. They
#: are kept at the standard defaults because they are the right values for the
#: regime this index is NOT currently in — a future catalog of long documents
#: would need them — and the sweep is the evidence for that claim.
BM25_K1 = 1.5
BM25_B = 0.75

#: A document whose term bag is empty contributes a length of 1 to the average
#: rather than 0. One empty document among forty would otherwise drag the mean
#: length down and inflate every other document's length penalty, which is a
#: real ranking distortion produced by a document that was never indexable.
_MIN_DOCUMENT_LENGTH = 1

#: Terms shorter than this are dropped. **Two, and that is a measured choice
#: against my own better argument.** The reasoning for a floor of 3 was that
#: at 2 the index carries ``of``/``in``/``is``/``it``, and with only 45 documents
#: those near-stopwords still earn IDF and win queries with no discriminative
#: term. Measured over a 28-query capability suite against this catalog:
#:
#: | floor | top-1 | top-3 |
#: |---|---:|---:|
#: | **2** | **22** | **24** |
#: | 3 | 21 | 23 |
#: | 4 | 21 | 23 |
#:
#: IDF does exactly the job it was supposed to: a term in most of a 45-document
#: corpus scores low, and dropping those terms only discards real matches.
#: The hypothesis was wrong and the measurement said so.
_MIN_TERM_CHARS = 2

#: CamelCase boundary: a lower/digit followed by an upper, or an acronym
#: followed by a Capitalised word (``HTTPServer`` -> ``HTTP``, ``Server``).
_CAMEL_BOUNDARY_A = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_BOUNDARY_B = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")

#: Anything that is not a letter, a digit, or an underscore is a separator.
#: Deliberately NOT ``\W``: underscore is a word character, and ``read_symbol``
#: must split on it, so it is excluded explicitly rather than leaning on
#: Unicode word semantics that differ between Python versions.
_SEPARATORS = re.compile(r"[^0-9A-Za-z]+")


def tokenize(text: Any) -> Tuple[str, ...]:
    """Split text into BM25 terms, lowercased.

    The splitting is the part most likely to be re-implemented, so the rules
    are stated here and pinned from tests rather than left to each caller:

    * ``read_symbol`` -> ``read``, ``symbol``
    * ``gitBlame`` / ``GitBlame`` -> ``git``, ``blame``
    * ``HTTPServer`` -> ``http``, ``server``
    * ``apply-patch`` -> ``apply``, ``patch``
    * ``harness/tools.py`` -> ``harness``, ``tools``, ``py``
    * ``A``, ``of``, ``in`` -> ``()`` (below :data:`_MIN_TERM_CHARS`)

    Deduplicated, order-preserving.

    **There is deliberately NO stemmer**, and that is a measured decision
    rather than an omission. The motivation was real — BM25 compares terms, so
    a model searching ``"who calls this function"`` scores ZERO against a
    description reading ``"its callers"``, a total miss on exactly the case
    deferred discovery exists to serve. A light stemmer was built and measured
    against this catalog's 28-query suite:

    | | top-1 | top-3 |
    |---|---:|---:|
    | no stemmer | **78.6%** | **85.7%** |
    | light stemmer | 78.6% | **82.1%** |

    It made the suite *worse*, and the reason is visible in its own output:
    the ``-es`` and bare-``-s`` rules manufacture junk forms — ``server`` ->
    ``serv``, ``files`` -> ``fil``, ``references`` -> ``referenc``,
    ``searches`` -> ``searche``. Junk terms are RARE, so IDF scores them HIGH,
    and a junk match therefore outranks a real one. Over-stemming does not
    merely fail to help here; it actively promotes noise, and promoting noise
    in a *tool search* means surfacing a tool that does not do the thing.

    The morphological gap is real and is addressed where it belongs: in the
    capability **sentences**, which are authored data and can say ``callers``
    when that is the word a model will use. :data:`_MIN_TERM_CHARS` is 3 for the
    same reason — the vocabulary belongs in the documents, not in a stemmer
    guessing at it.
    """
    raw = str(text or "")
    if not raw:
        return ()
    spaced = _CAMEL_BOUNDARY_B.sub(" ", _CAMEL_BOUNDARY_A.sub(" ", raw))
    terms: List[str] = []
    seen: set = set()
    for chunk in _SEPARATORS.split(spaced):
        term = chunk.strip().lower()
        if len(term) < _MIN_TERM_CHARS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return tuple(terms)


@dataclass(frozen=True)
class SearchDocument:
    """One indexable document: a key plus weighted named fields.

    ``fields`` maps a field name to ``(text, weight)``. Weighting is how a name
    hit outranks a description hit WITHOUT the ranker knowing anything about
    what a "name" is — the caller owns that judgement, which is what lets this
    same class serve tools, skills, and a future MCP catalog unchanged.

    A field may also be given as a bare string, in which case
    :data:`DEFAULT_FIELD_WEIGHT` is used.
    """

    key: str
    fields: Tuple[Tuple[str, Tuple[str, float]], ...] = ()

    @staticmethod
    def of(key: Any, /, **fields: Any) -> "SearchDocument":
        """Build a document from keyword fields, string or ``(text, weight)``.

        ``SearchDocument.of("read", name="read", description="Read a file")``
        """
        normalised: List[Tuple[str, Tuple[str, float]]] = []
        for field_name, value in fields.items():
            if value is None:
                continue
            if isinstance(value, tuple) and len(value) == 2:
                text, weight = value
                try:
                    weight_value = float(weight)
                except (TypeError, ValueError):
                    weight_value = DEFAULT_FIELD_WEIGHT
            else:
                text, weight_value = value, DEFAULT_FIELD_WEIGHT
            if not str(text or "").strip():
                continue
            normalised.append(
                (str(field_name), (str(text), weight_value))
            )
        return SearchDocument(key=str(key), fields=tuple(normalised))

    def term_counts(self) -> Dict[str, float]:
        """Return the weighted term frequency bag for this document."""
        bag: Dict[str, float] = {}
        for _field_name, (text, weight) in self.fields:
            for term in tokenize(text):
                bag[term] = bag.get(term, 0.0) + float(weight)
        return bag

    def length(self) -> int:
        """Return the document's unweighted token length.

        Unweighted on purpose. BM25's length normalisation is about how much
        TEXT a document has; counting a 3x-weighted name term as three tokens
        would make every document with a long name look long, and the penalty
        would then be applied to exactly the documents that deserve it least.
        """
        total = 0
        for _field_name, (text, _weight) in self.fields:
            total += len(tokenize(text))
        return max(_MIN_DOCUMENT_LENGTH, total)

    def searchable_text(self) -> str:
        """Return every field's text joined, for display and for de-bugging.

        Never used for scoring — :meth:`term_counts` is — so a caller that
        wants to print "why did this match" has the bytes without the ranking
        depending on their formatting.
        """
        return " ".join(text for _name, (text, _weight) in self.fields)


#: The weight a bare string field receives. Chosen so that a bare
#: ``description="..."`` field is a weak signal by default: the caller who
#: cares about ranking strength names the weight explicitly.
DEFAULT_FIELD_WEIGHT = 1.0

#: The weight used for a tool or skill NAME. High, because an exact name hit is
#: almost always what the model meant, and because the corpus's direct-selection
#: syntax (``select:<tool_name>``) exists precisely because a name match should
#: not have to compete with description words.
NAME_FIELD_WEIGHT = 8.0

#: The weight used for a description. Above 1.0 so a genuine description-word
#: hit is worth having at all, which is the whole point — but far below
#: :data:`NAME_FIELD_WEIGHT`, so an incidental name hit cannot bury the
#: document whose DESCRIPTION answers the query.
DESCRIPTION_FIELD_WEIGHT = 2.0

#: The weight for a keyword/tag field. Between the two: tags are curated labels
#: rather than prose, so they are closer to a name than to a description.
TAG_FIELD_WEIGHT = 4.0


@dataclass(frozen=True)
class BM25Result:
    """One ranked hit, with the evidence that produced the score.

    ``matched_terms`` is not decoration. "Why did this come first" is the
    question a receipt reader asks, and a score with no explanation attached
    cannot be defended — the repository's standing rule is that a receipt
    cannot lie, and an unexplainable rank is a claim.
    """

    key: str
    score: float
    matched_terms: Tuple[str, ...] = ()
    document: Optional[SearchDocument] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "key": self.key,
            "score": round(float(self.score), 6),
            "matched_terms": list(self.matched_terms),
        }


class BM25Index:
    """A BM25 index over weighted named fields. Pure and deterministic.

    Parameters
    ----------
    documents:
        The corpus. Duplicated keys are a :class:`ValueError` rather than a
        silent last-wins: two documents claiming one key means the caller
        merged two vocabularies, and a ranker that quietly drops one of them
        reports a confident answer about a corpus it did not index.
    k1, b:
        The BM25 free parameters, defaulting to :data:`BM25_K1` / :data:`BM25_B`.

    Notes
    -----
    IDF is the **Robertson/Sparck-Jones positive** form,
    ``ln(1 + (N - df + 0.5) / (df + 0.5))``. The ``1 +`` matters and the reason
    is specific to this use: every tool description in the catalog shares
    vocabulary with every other one ("file", "run", "search"), so several terms
    are genuinely common. The unclamped ``ln((N-df+0.5)/(df+0.5))`` form goes
    NEGATIVE for a term in more than half the corpus, which would rank a
    document DOWN for containing a term the query asked for. Clamping at zero
    also works but discards the magnitude; the positive form keeps the ordering
    among the informative terms.
    """

    __slots__ = (
        "_avg_length",
        "_b",
        "_bags",
        "_df",
        "_doc_lengths",
        "_documents",
        "_k1",
        "_keys",
        "_n",
    )

    def __init__(
        self,
        documents: Iterable[SearchDocument],
        *,
        k1: float = BM25_K1,
        b: float = BM25_B,
    ) -> None:
        """Build the index. Raises ``ValueError`` on a duplicate key."""
        keys: List[str] = []
        documents_kept: List[SearchDocument] = []
        bags: List[Dict[str, float]] = []
        lengths: List[int] = []
        df: Dict[str, int] = {}
        seen: set = set()
        for document in documents or ():
            key = str(getattr(document, "key", "") or "")
            if not key:
                raise ValueError("a search document needs a non-empty key")
            if key in seen:
                raise ValueError(f"duplicate search document key: {key}")
            seen.add(key)
            bag = document.term_counts()
            keys.append(key)
            documents_kept.append(document)
            bags.append(bag)
            lengths.append(document.length())
            for term in bag:
                df[term] = df.get(term, 0) + 1
        self._keys = keys
        self._documents = documents_kept
        self._bags = bags
        self._doc_lengths = lengths
        self._df = df
        self._n = len(keys)
        self._k1 = float(k1)
        self._b = float(b)
        total = sum(lengths)
        self._avg_length = (total / self._n) if self._n else 0.0

    # -- introspection ----------------------------------------------------

    def __len__(self) -> int:
        """Return the number of indexed documents."""
        return self._n

    @property
    def keys(self) -> Tuple[str, ...]:
        """Return every document key in insertion order."""
        return tuple(self._keys)

    def document(self, key: Any) -> Optional[SearchDocument]:
        """Return the indexed document for ``key``, or ``None``."""
        wanted = str(key or "")
        for position, candidate in enumerate(self._keys):
            if candidate == wanted:
                return self._documents[position]
        return None

    @property
    def average_length(self) -> float:
        """Return the mean document length used for length normalisation."""
        return float(self._avg_length)

    def document_frequency(self, term: Any) -> int:
        """Return how many documents contain ``term``, or 0."""
        return int(self._df.get(str(term or "").strip().lower(), 0))

    def idf(self, term: Any) -> float:
        """Return the positive-form IDF of ``term`` in this corpus."""
        cleaned = str(term or "").strip().lower()
        if not cleaned or not self._n or cleaned not in self._df:
            return 0.0
        df = self._df[cleaned]
        return math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))

    # -- scoring -----------------------------------------------------------

    def score_document(self, index: int, query_terms: Sequence[str]) -> float:
        """Return the BM25 score of one document against ``query_terms``."""
        if not query_terms or index >= self._n:
            return 0.0
        bag = self._bags[index]
        if not bag:
            return 0.0
        length = float(self._doc_lengths[index])
        avg = self._avg_length or 1.0
        total = 0.0
        for term in query_terms:
            frequency = bag.get(term)
            if not frequency:
                continue
            weight = self.idf(term)
            denominator = frequency + self._k1 * (
                1.0 - self._b + self._b * (length / avg)
            )
            if denominator <= 0.0:
                continue
            total += weight * (frequency * (self._k1 + 1.0)) / denominator
        return total

    def search(
        self,
        query: Any,
        *,
        limit: int = 10,
        candidates: Optional[Iterable[str]] = None,
    ) -> List[BM25Result]:
        """Return ranked hits for ``query``, best first.

        ``limit <= 0`` returns ``[]`` — an explicit "no results wanted" is a
        real request, and silently returning the whole corpus for it is how a
        bounded search becomes an unbounded one.

        ``candidates`` restricts the ranked set to a subset of keys WITHOUT
        changing the statistics. This is the deferral mechanism's most
        important property: an index built over all 45 tools, searched with
        ``candidates`` limited to the 7 deferred ones, ranks the deferred seven
        against each other *and* against the corpus IDF the whole catalog
        implies. Re-indexing the subset instead would give every term an IDF
        computed from a 7-document corpus, which would inflate all of them and
        change the ORDER — a search whose result order depends on how many
        candidates you passed is not a search.
        """
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 10
        if cap <= 0 or not self._n:
            return []
        terms = tokenize(query)
        if not terms:
            # An empty query is not "match everything, ranked arbitrarily". It
            # is a query with no information, and returning an arbitrary
            # ordering under that label is a receipt that looks like a result.
            return []
        allowed = None
        if candidates is not None:
            allowed = {str(item) for item in candidates}
            if not allowed:
                return []
        positions = range(self._n)
        keyed: List[Tuple[str, int]] = [(self._keys[i], i) for i in positions]
        scored: List[Tuple[float, str, int, Tuple[str, ...]]] = []
        for key, position in keyed:
            if allowed is not None and key not in allowed:
                continue
            score = self.score_document(position, terms)
            if score <= 0.0:
                continue
            matched = tuple(term for term in terms if self._bags[position].get(term))
            scored.append((score, key, position, matched))
        # Sort by score DESC, then key ASC. The tiebreak is what makes the
        # output order reproducible; without it two equally-scoring documents
        # come back in whatever order the corpus happened to be built in.
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [
            BM25Result(
                key=key,
                score=float(score),
                matched_terms=matched,
                document=self._documents[position],
            )
            for score, key, position, matched in scored[:cap]
        ]


def bm25(
    documents: Iterable[SearchDocument],
    query: Any,
    *,
    limit: int = 10,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> List[BM25Result]:
    """Build an index over ``documents`` and search it in one call.

    A convenience for one-shot use. A caller that searches the same corpus
    more than once MUST hold a :class:`BM25Index` instead — building it per
    query is O(corpus) per search, which for a per-turn discovery path is the
    difference between a free search and a noticeable one.
    """
    return BM25Index(documents, k1=k1, b=b).search(query, limit=limit)
