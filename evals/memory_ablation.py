"""The long-term-memory ablation arm, and the four metrics it measures.

WHY THIS IS A SEPARATE MODULE AND NOT AN ``evals/run.py`` ARM
-----------------------------------------------------------
The existing ``evals.run.ARMS`` table cannot hold this arm, and the reason is a
contract rather than a preference:

* ``tests/test_evals_tasks.py::test_runner_arms_use_real_config_keys`` requires
  EVERY key in EVERY arm to exist in ``harness.config.DEFAULTS``;
* ``harness/config.py`` is not this terminal's file -- ``harness/**`` is
  explicitly out of scope -- so a new key cannot be registered there;
* and the phase's own non-negotiable is **"never add a truthy ``DEFAULTS`` key"**,
  because a value in ``DEFAULTS`` is merged into every task and every eval arm
  and would silently switch every run in the project onto the new architecture.

So the arm is driven by KEY PRESENCE, exactly as ``code_graph_verify_digests``
and ``verification_*`` already are, and it lives here rather than in the
``ARMS`` dict. :data:`MEMORY_ARMS` is the arm table; :func:`arm_config` produces
the config each arm runs with. :func:`test_arm_keys_are_absent_from_defaults`
lives in ``tests/test_p3a_memory_ablation.py`` and fails if any key ever
appears in ``DEFAULTS`` -- the pin that keeps this decision honest as the repo
grows.

WHAT THE ARM ACTUALLY MEASURES, AND WHAT IT DOES NOT
-----------------------------------------------------
It measures the RECALL pipeline end to end, deterministically and offline:
seed a store, index it, capture a frozen snapshot, cite what a hypothetical run
would have used, and score four metrics against LABELS we supply. It runs no
model and no Docker.

It does NOT measure whether recalling memory makes the agent better at fixing
bugs. That needs the full loop and a real provider, and this module's report
says so in ``claims``. A recall@k of 1.0 with a scripted model proves the
retrieval surface works; it says nothing about task success, and the report is
built so that a reader cannot read the first number as the second.

THE FOUR METRICS, AND WHY EACH ONE CAN BE ``None``
---------------------------------------------------
``recall_at_k``            did the ranking surface the memories a query needed?
``citation_rate``          of the memories injected, how many were used?
``stale_memory_rate``      how much of memory has gone uncited for too long?
``decision_recurrence_rate`` did the agent re-take a decision memory already held?

Every one of them can return ``None`` with a reason, because each has a
legitimate "we did not measure it" case and none of those cases is a zero. A
citation rate over zero injected memories is not 0% -- nobody took the
measurement. The report carries ``vacuous`` alongside every metric for the same
reason.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from memory.decision_store import DecisionStore
from memory.ranking import (
    UsageIndex,
    citation_rate,
    decision_recurrence_rate,
    rank_memories,
    recall_at_k,
    stale_memory_rate,
)
from memory.search_index import SearchIndex
from memory.snapshot import capture_snapshot

__all__ = [
    "ARM_KEYS",
    "LABELLED_QUERIES",
    "MEMORY_ARMS",
    "MemoryAblationReport",
    "arm_config",
    "memory_ablation_report",
    "memory_enabled",
    "render",
]

#: The config keys this module reads. Read by KEY PRESENCE and explicit
#: truthiness; deliberately absent from ``harness.config.DEFAULTS``.
#:
#: All three are FEATURE keys: each is switched on by the baseline arm and off
#: by exactly one isolating arm. A key that no arm switches would be a label
#: pretending to be a feature, and the gate's ``r2_ablation_arms`` rung requires
#: the baseline to turn every one of these ON -- which is how a dead key in this
#: table was caught.
ARM_KEYS: Tuple[str, ...] = (
    "memory_recall_enabled",
    "memory_pipeline_enabled",
    "memory_ranking_enabled",
)

#: The truthy strings accepted for an explicit boolean. A settings file with
#: ``"False"`` as a STRING is truthy in Python, which is why this list exists:
#: ``memory_recall_enabled: "False"`` must mean OFF, not ON.
_TRUTHY = {"1", "true", "yes", "on", "enable", "enabled"}
_FALSEY = {"0", "false", "no", "off", "disable", "disabled", "none", ""}

#: The arm table.
#:
#: EVERY arm declares its FULL key set, and that is load-bearing rather than
#: verbose. Configuration here is read by KEY PRESENCE, so an absent key means
#: OFF. An arm that set only the key it wanted to change would therefore also
#: switch every other feature off, and ``no_ranking`` would silently become
#: ``no_recall_and_no_ranking`` -- measuring nothing about ranking. That bug
#: existed in the first draft of this table and the suite caught it by reporting
#: ``recall@k: not measured`` for the ranking arm.
MEMORY_ARMS: Dict[str, Dict[str, Any]] = {
    # everything the phase built, on
    "memory_on": {
        "memory_recall_enabled": True,
        "memory_pipeline_enabled": True,
        "memory_ranking_enabled": True,
    },
    # recall off: no snapshot is injected at all
    "no_recall": {
        "memory_recall_enabled": False,
        "memory_pipeline_enabled": True,
        "memory_ranking_enabled": True,
    },
    # ranking off ONLY: memory is still injected, in raw recency order with no
    # citation weighting. This is the arm that isolates RANKING from RECALL.
    "no_ranking": {
        "memory_recall_enabled": True,
        "memory_pipeline_enabled": True,
        "memory_ranking_enabled": False,
    },
    # the pipeline off: extraction, inbox and consolidation are inert, so a
    # session records nothing durable. Recall and ranking stay on, so this arm
    # isolates the WRITE side.
    "no_pipeline": {
        "memory_recall_enabled": True,
        "memory_pipeline_enabled": False,
        "memory_ranking_enabled": True,
    },
    # the pre-phase surface, wholesale
    "pre_phase": {
        "memory_recall_enabled": False,
        "memory_pipeline_enabled": False,
        "memory_ranking_enabled": False,
    },
}

#: The labelled query set the recall metric is scored against.
#:
#: Each entry names a QUERY, the ids a correct retriever must surface, and the
#: gold set. ``expect_ids`` are the memories whose text contains the query's
#: distinctive terms; ``gold_ids`` are the ones a caller actually needed.
#:
#: The distinction matters and is why both fields exist. A retriever that
#: surfaces everything relevant but ranks a noisy row first has partial recall;
#: scoring only against ``gold_ids`` hides that, and scoring only against
#: ``expect_ids`` flatters a retriever that returned the whole corpus.
LABELLED_QUERIES: Tuple[Dict[str, Any], ...] = (
    {
        "query": "pytest invocation",
        "gold_ids": ("pytest_root",),
        "note": "the per-repo test invocation convention",
    },
    {
        "query": "mean denominator",
        "gold_ids": ("mean_len",),
        "note": "the arithmetic-mean semantics gotcha",
    },
    {
        "query": "yaml dependency",
        "gold_ids": ("yaml_missing",),
        "note": "a missing optional dependency in the CI image",
    },
    {
        "query": "commit message style",
        "gold_ids": ("commit_style",),
        "note": "a USER-STATED preference, which must rank in the user tier",
    },
    {
        "query": "sed quoting",
        "gold_ids": ("sed_quoting",),
        "note": "a tooling gotcha with no lexical overlap with its own name",
    },
)

#: The seeded corpus. Four of the rows are also retrievable by ID so a test can
#: assert against a known id; every row's text is deliberately close to the
#: labelled queries so the metric is measuring RECALL rather than luck.
SEED_ROWS: Tuple[Dict[str, Any], ...] = (
    {
        "doc_id": "pytest_root",
        "category": "convention",
        "source": "agent",
        "text": "Run the test suite with `python -m pytest` from the repository root; bare `pytest` cannot import the package under test.",
    },
    {
        "doc_id": "mean_len",
        "category": "gotcha",
        "source": "agent",
        "text": "mean() divides by len, not len-1: the arithmetic mean, not the sample mean.",
    },
    {
        "doc_id": "yaml_missing",
        "category": "gotcha",
        "source": "issue",
        "untrusted_source": True,
        "text": "The CI image has no PyYAML installed, so yaml config tests fail at import with ModuleNotFoundError.",
    },
    {
        "doc_id": "commit_style",
        "category": "preference",
        "source": "manual",
        "text": "The user prefers descriptive commit message subjects in the imperative mood.",
    },
    {
        "doc_id": "sed_quoting",
        "category": "gotcha",
        "source": "agent",
        "text": "sed replacement strings containing quotes break on some shells; prefer the python -c form.",
    },
    {
        "doc_id": "noise_license",
        "category": "general",
        "source": "agent",
        "text": "The project is MIT licensed; see LICENSE for the full text.",
    },
    {
        "doc_id": "noise_colors",
        "category": "general",
        "source": "agent",
        "text": "The brand palette is defined in shared/brand.py and shared/policy_data.",
    },
)


def _flag(value: Any) -> Optional[bool]:
    """Read one config value as an explicit tri-state boolean.

    ``None`` means ABSENT. Absent is not False and absent is not True: the whole
    point of key-presence configuration is that an absent key means the caller
    has not decided, and a default that decided for them would switch every arm.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in _TRUTHY:
        return True
    if text in _FALSEY:
        return False
    # An unrecognised value is None, not False. Treating a typo as OFF would
    # make a misspelled key silently disable the feature the operator asked for,
    # and treating it as ON would enable one they did not.
    return None


def memory_enabled(config: Optional[Mapping[str, Any]], key: str) -> bool:
    """Return whether ``key`` is present AND explicitly truthy.

    The two conditions are load-bearing and both are required:

    * **presence** -- an absent key is off, so a task that never mentioned
      memory cannot acquire the architecture by accident;
    * **explicit truthiness** -- so ``{"key": "False"}`` and a typo are not ON.

    An unrecognised value is OFF and :func:`unusable_value` names it, because
    "off because you misspelled it" and "off because you did not ask" are
    different answers.
    """
    if not isinstance(config, MappingABC):
        return False
    return _flag(config.get(key)) is True


def unusable_value(config: Optional[Mapping[str, Any]], key: str) -> bool:
    """Whether ``key`` is present with a value this module cannot interpret."""
    if not isinstance(config, MappingABC) or key not in config:
        return False
    return _flag(config.get(key)) is None


def arm_config(arm: str) -> Dict[str, Any]:
    """Return the config an arm runs with. Unknown arms raise."""
    if arm not in MEMORY_ARMS:
        raise KeyError(
            f"unknown memory arm {arm!r}; this suite defines {sorted(MEMORY_ARMS)!r}"
        )
    return dict(MEMORY_ARMS[arm])


@dataclass(frozen=True)
class MemoryAblationReport:
    """One arm's measured result, with the honest-absent rules applied."""

    arm: str
    config: Mapping[str, Any]
    recall: Mapping[str, Any]
    citation: Mapping[str, Any]
    stale: Mapping[str, Any]
    recurrence: Mapping[str, Any]
    ranking_receipt: Mapping[str, Any]
    search_engine: str
    search_available: bool
    injected: int
    cited: int
    store_rows: int
    vacuous: Mapping[str, bool]
    notes: Tuple[str, ...]
    claims: Tuple[str, ...]
    engine: str
    model_calls: int
    at: str

    @property
    def measured(self) -> bool:
        """Whether at least one metric was actually measured.

        ``False`` means the arm produced numbers nobody took. The verdict
        derivation requires this, so an arm that silently measured nothing can
        never read as a pass.
        """
        return bool(
            self.recall.get("measured")
            or self.citation.get("citation_rate") is not None
            or self.stale.get("stale_memory_rate") is not None
            or self.recurrence.get("measured")
        )

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible report."""
        return {
            "arm": self.arm,
            "config": dict(self.config),
            "metrics": {
                "recall_at_k": self.recall,
                "citation_rate": self.citation,
                "stale_memory_rate": self.stale,
                "decision_recurrence_rate": self.recurrence,
            },
            "ranking": dict(self.ranking_receipt),
            "search": {
                "engine": self.search_engine,
                "available": self.search_available,
            },
            "injected": self.injected,
            "cited": self.cited,
            "store_rows": self.store_rows,
            "vacuous": dict(self.vacuous),
            "measured": self.measured,
            "notes": list(self.notes),
            "claims": list(self.claims),
            "engine": self.engine,
            "model_calls": self.model_calls,
            "at": self.at,
        }


def _seed_corpus(
    store: DecisionStore, index: SearchIndex, repo_path: Path
) -> Dict[str, int]:
    """Seed the store and its search index from :data:`SEED_ROWS`.

    Returns the ``doc_id -> store row id`` map, which the metric code uses as
    the id space. The seed goes through the store's normal ``record`` API --
    not through the inbox -- because this arm measures RECALL of memory that
    already exists, and routing the seed through a human approval step would
    measure the inbox instead.
    """
    ids: Dict[str, int] = {}
    documents: List[Dict[str, Any]] = []
    for row in SEED_ROWS:
        provenance = {"kind": "ablation-seed", "source": row["source"], "explicit": True}
        if row.get("untrusted_source"):
            provenance["untrusted_source"] = True
        row_id = store.record(
            row["text"],
            category=row["category"],
            source=row["source"],
            repo_path=str(repo_path),
            provenance=provenance,
        )
        if row_id is None:
            continue
        ids[row["doc_id"]] = int(row_id)
        documents.append(
            {
                "doc_id": row["doc_id"],
                "title": f"{row['category']} memory",
                "body": row["text"],
                "repo_key": str(repo_path),
                "updated_at": f"2026-01-{len(ids):02d}T00:00:00Z",
            }
        )
    index.index("memories", documents)
    return ids


def _seed_prior_decisions(store: DecisionStore, repo_path: Path) -> List[str]:
    """Seed a PRIOR run's decisions, for the recurrence metric.

    Recurrence is measured against a second observation, so the arm needs one.
    The prior run took TWO decisions; :func:`_seed_current_run` re-takes ONE of
    them. That asymmetry is deliberate: a suite where the current run re-takes
    everything reports 1.0 and measures nothing, and one where it re-takes
    nothing reports 0.0 and equally measures nothing.
    """
    texts = [
        "chose a full-file rewrite over a diff for the mean() fix",
        "the arithmetic mean divides by len, not len-1",
    ]
    for text in texts:
        store.record(
            text,
            category="task",
            source="state-file",
            repo_path=str(repo_path),
            provenance={"kind": "state-file", "source": "state-file", "explicit": True},
        )
    return list(texts)


def _seed_current_run(store: DecisionStore, repo_path: Path) -> List[str]:
    """Seed the CURRENT run's decisions: one re-taken, one genuinely new.

    Returns the texts to score recurrence against. The re-taken one is the
    RECURRENCE FAILURE memory should have prevented; the new one is what a
    correct run looks like. The metric is only interpretable because both are
    present.
    """
    texts = [
        # RE-TAKEN: this is the failure the metric counts.
        "the arithmetic mean divides by len, not len-1",
        # NEW: not previously held, so not a recurrence.
        "replaced the shared helper rather than duplicating it per module",
    ]
    for text in texts:
        store.record(
            text,
            category="task",
            source="state-file",
            repo_path=str(repo_path),
            provenance={"kind": "state-file", "source": "state-file", "explicit": True},
        )
    return list(texts)


def _run_one_arm(arm: str, *, workdir: Path, k: int) -> MemoryAblationReport:
    """Run ONE arm in an isolated workspace and measure it.

    Each arm gets its own temporary store, index and usage DB, so arms cannot
    contaminate one another -- a shared store would make the second arm's
    citations count towards the first arm's recall.
    """
    config = arm_config(arm)
    notes: List[str] = []
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for key in ARM_KEYS:
        if unusable_value(config, key):
            notes.append(
                f"config key {key!r} carries a value this suite cannot "
                "interpret, so it was read as OFF"
            )

    recall_on = memory_enabled(config, "memory_recall_enabled")
    ranking_on = memory_enabled(config, "memory_ranking_enabled")

    arm_root = workdir / arm
    arm_root.mkdir(parents=True, exist_ok=True)
    store = DecisionStore(str(arm_root / "decisions.db"))
    index = SearchIndex(str(arm_root / "index.db"))
    usage = UsageIndex(str(arm_root / "usage.db"))
    repo_path = arm_root

    try:
        ids = _seed_corpus(store, index, repo_path)
        prior_decisions = _seed_prior_decisions(store, repo_path)

        # -- recall, measured through the REAL frozen snapshot ---------
        snapshot = capture_snapshot(
            store, repo_path=str(repo_path), usage=usage, session_id="ablation"
        )
        injected_ids = [int(row.memory_id) for row in snapshot.rows] if recall_on else []
        if not recall_on:
            notes.append(
                "recall is OFF in this arm, so no memory was injected and the "
                "recall metric below is reported as NOT MEASURED rather than as "
                "a score of zero"
            )

        # -- citation: a hypothetical run cites the top-k it was given --
        cited_ids: List[int] = []
        if recall_on and injected_ids:
            # The arm's citation model, stated rather than hidden: a run cites
            # what it acted on. Without a real loop there is no way to observe
            # this, so it is INJECTED here and the report says so.
            cited_ids = injected_ids[: max(1, len(injected_ids) // 2)]
            usage.cite_many(cited_ids, session_id="ablation", run_id="ablation-run")

        citation = citation_rate(injected_ids, cited_ids)
        if citation.get("vacuous"):
            notes.append(citation["reason"])

        # -- the ranking receipt, with its usage_applied flag ----------
        ranked = rank_memories(
            store.search("", limit=10, repo_path=str(repo_path)),
            usage=usage,
            report_ranking=ranking_on,
        )
        if ranking_on and not ranked.usage_applied:
            notes.append(
                "ranking was requested but the usage term did not apply: "
                + ranked.usage_reason
            )
        if not ranking_on:
            notes.append(
                "ranking is OFF in this arm, so the reported order is raw "
                "recency and no citation weighting was applied"
            )

        # -- recall over the LABELLED query set -------------------------
        #
        # The retrieval leg is the REAL one: `index.search` is the FTS5 engine
        # the product ships, so the engine that answered is on the receipt. The
        # ordering leg is the snapshot's rank, so a memory the engine surfaced
        # but the ranking buried still counts as not-recalled at small k --
        # which is the pair (retrieval, ranking) the exit gate is about.
        name_to_id = {name: value for name, value in ids.items()}
        snapshot_order: Dict[int, int] = {}
        receipt = snapshot.ranking
        if receipt is not None:
            snapshot_order = {
                int(row.memory_id): int(row.rank) for row in receipt.ranked
            }
        per_query: List[Dict[str, Any]] = []
        measured = 0
        hits_total = 0
        for labelled in LABELLED_QUERIES:
            outcome = index.search(
                labelled["query"],
                kind="memories",
                limit=10,
                repo_key=str(repo_path),
                match_mode="or",
            )
            surfaced = [
                name_to_id[hit.doc_id]
                for hit in outcome.hits
                if hit.doc_id in name_to_id
            ]
            gold = [ids[name] for name in labelled["gold_ids"] if name in ids]
            if recall_on:
                # Order the surfaced memories by the snapshot's rank, so the
                # top-k is the order the RUN would actually have seen.
                retrieved = sorted(surfaced, key=lambda value: snapshot_order.get(value, 10**6))
            else:
                retrieved = []
            scored = recall_at_k(retrieved, gold, k=k)
            scored["query"] = labelled["query"]
            scored["note"] = labelled["note"]
            scored["engine"] = outcome.engine
            scored["search_available"] = outcome.available
            scored["surfaced"] = len(surfaced)
            if not outcome.available:
                scored["reason"] = (
                    f"{scored['reason']} SEARCH ENGINE UNAVAILABLE: {outcome.reason}"
                ).strip()
            per_query.append(scored)
            if scored.get("measured"):
                measured += 1
                hits_total += int(scored.get("hits") or 0)

        recall: Dict[str, Any]
        if not recall_on:
            recall = {
                "recall_at_k": None,
                "k": k,
                "measured": False,
                "queries": per_query,
                "reason": (
                    "recall is OFF in this arm; no memory was injected, so recall "
                    "was not measured. This is not a recall of zero."
                ),
            }
        elif measured == 0:
            recall = {
                "recall_at_k": None,
                "k": k,
                "measured": False,
                "queries": per_query,
                "reason": "no query in the labelled set could be scored",
            }
        else:
            gold_total = sum(
                int(item.get("gold") or 0)
                for item in per_query
                if item.get("measured")
            )
            recall = {
                "recall_at_k": round(hits_total / gold_total, 6) if gold_total else None,
                "k": k,
                "measured": True,
                "queries_measured": measured,
                "queries_total": len(per_query),
                "queries": per_query,
                "reason": "",
            }

        # -- staleness of the usage corpus -------------------------------
        usage_rows = usage.usage(sorted(ids.values()) or [0])
        stale = stale_memory_rate(usage_rows)
        if stale.get("vacuous"):
            notes.append(stale["reason"])

        # -- recurrence: did the run re-take a held decision? ------------
        current = _seed_current_run(store, repo_path)
        recurrence = decision_recurrence_rate(prior_decisions, current)
        if not recurrence.get("measured"):
            notes.append(recurrence["reason"])

        stats = index.stats()
        claims = (
            "This arm measures the RECALL pipeline: seeding, FTS5 retrieval, "
            "ranking, frozen-snapshot injection and the metrics over them.",
            "It does NOT measure task success, model quality or cost. No model "
            "call was made anywhere in this suite.",
            "The citation set is INJECTED by the arm rather than observed from a "
            "run, because there is no loop here to observe it in. A real citation "
            "rate requires the full harness with a real provider.",
            "A recall@k of 1.0 proves the retrieval surface returns the labelled "
            "memories; it is not evidence that recalling them fixes bugs.",
        )
        return MemoryAblationReport(
            arm=arm,
            config=config,
            recall=recall,
            citation=citation,
            stale=stale,
            recurrence=recurrence,
            ranking_receipt=ranked.as_dict(),
            search_engine=str(stats.get("engine") or ""),
            search_available=bool(stats.get("available")),
            injected=len(injected_ids),
            cited=len(cited_ids),
            store_rows=store.count(include_revoked=True),
            vacuous={
                "recall": not bool(recall.get("measured")),
                "citation": bool(citation.get("vacuous")),
                "stale": bool(stale.get("vacuous")),
                "recurrence": not bool(recurrence.get("measured")),
            },
            notes=tuple(notes),
            claims=claims,
            engine="deterministic",
            model_calls=0,
            at=at,
        )
    finally:
        usage.close()
        index.close()
        store.close()


def memory_ablation_report(
    *, arms: Optional[Sequence[str]] = None, k: int = 5, workdir: Optional[Any] = None
) -> Dict[str, Any]:
    """Run the memory arms and return a machine-readable report.

    ``arms`` defaults to every arm in :data:`MEMORY_ARMS`. ``workdir`` defaults
    to a fresh temporary directory that is REMOVED afterwards, so running the
    suite leaves nothing behind in the user's tree.
    """
    selected = list(arms) if arms else list(MEMORY_ARMS)
    unknown = [name for name in selected if name not in MEMORY_ARMS]
    if unknown:
        raise KeyError(
            f"unknown memory arm(s) {unknown!r}; this suite defines "
            f"{sorted(MEMORY_ARMS)!r}"
        )
    temporary = workdir is None
    root = Path(str(workdir)) if workdir is not None else Path(tempfile.mkdtemp())
    try:
        results = {
            name: _run_one_arm(name, workdir=root, k=k).as_dict() for name in selected
        }
    finally:
        if temporary:
            shutil.rmtree(root, ignore_errors=True)

    recall_arm = results.get("memory_on", {}).get("metrics", {}).get("recall_at_k", {})
    baseline_recall = (
        recall_arm.get("recall_at_k") if isinstance(recall_arm, dict) else None
    )
    any_measured = any(
        bool((payload or {}).get("measured")) for payload in results.values()
    )
    engine_named = all(
        str((payload or {}).get("search", {}).get("engine") or "") for payload in results.values()
    )
    errors: List[Dict[str, str]] = []
    if not any_measured:
        errors.append(
            {
                "code": "no_arm_measured_anything",
                "message": (
                    "no arm produced a measured metric, so this report asserts "
                    "nothing; a suite whose every number is None is not a pass"
                ),
            }
        )
    if not engine_named:
        errors.append(
            {
                "code": "search_engine_unnamed",
                "message": (
                    "at least one arm did not report a search engine; the engine "
                    "must be named on every path, including the unavailable one"
                ),
            }
        )
    return {
        "suite": "memory-ablation",
        "phase": "P3a",
        "k": int(k),
        "match_mode": "or",
        "match_mode_reason": (
            "recall is scored in 'or' mode: an AND search over a labelled query "
            "set whose second term is a paraphrase measures the labelling "
            "rather than the retrieval, and would report a recall of zero for "
            "a query the corpus plainly answers"
        ),
        "arms": results,
        "arm_names": selected,
        "keys": list(ARM_KEYS),
        "keys_in_harness_defaults": _keys_in_defaults(),
        "baseline_arm": "memory_on",
        "baseline_recall_at_k": baseline_recall,
        "labelled_queries": len(LABELLED_QUERIES),
        "deterministic": True,
        "model_calls": 0,
        "docker": False,
        "network": False,
        "claims": [
            "measures the recall pipeline, not task success",
            "no model call and no Docker lane ran",
            "citations are injected by the arm, not observed from a run",
        ],
        "errors": errors,
        "verdict": "CLEAN" if not errors else "ERROR",
    }


def _keys_in_defaults() -> Dict[str, bool]:
    """Report which arm keys are present in ``harness.config.DEFAULTS``.

    Every value must be ``False``. A truthy default would be merged into every
    task and every eval arm, silently switching the whole project onto this
    architecture -- which is the specific failure the phase forbids.
    """
    try:
        from harness.config import DEFAULTS
    except Exception:
        return {key: False for key in ARM_KEYS}
    return {key: key in DEFAULTS for key in ARM_KEYS}


def render(report: Mapping[str, Any]) -> str:
    """Render the report for a terminal, with the honesty rules visible."""
    lines = [
        "memory ablation (deterministic, offline, no model call, no Docker)",
        "",
        f"search engine: {json.dumps({name: (payload or {}).get('search') for name, payload in (report.get('arms') or {}).items()})}",
        f"arm keys in harness DEFAULTS: "
        f"{json.dumps(report.get('keys_in_harness_defaults') or {})}",
        "",
        "| arm | recall@k | cited | stale | recurrent | measured |",
        "|---|---|---|---|---|---|",
    ]
    for name in report.get("arm_names") or []:
        payload = (report.get("arms") or {}).get(name) or {}
        metrics = payload.get("metrics") or {}

        def cell(value: Any) -> str:
            if value is None:
                return "n/m"
            if isinstance(value, float):
                return f"{value:.3f}"
            return str(value)

        lines.append(
            f"| {name} "
            f"| {cell(metrics.get('recall_at_k', {}).get('recall_at_k'))} "
            f"| {cell(metrics.get('citation_rate', {}).get('citation_rate'))} "
            f"| {cell(metrics.get('stale_memory_rate', {}).get('stale_memory_rate'))} "
            f"| {cell(metrics.get('decision_recurrence_rate', {}).get('decision_recurrence_rate'))} "
            f"| {payload.get('measured')} |"
        )
    lines.append("")
    lines.append("n/m = NOT MEASURED (the arm had nothing to measure; not a zero)")
    lines.append("")
    for name in report.get("arm_names") or []:
        payload = (report.get("arms") or {}).get(name) or {}
        for note in payload.get("notes") or []:
            lines.append(f"  [{name}] {note}")
    if report.get("errors"):
        lines.append("")
        for error in report["errors"]:
            lines.append(f"ERROR {error.get('code')}: {error.get('message')}")
    lines.append("")
    for claim in report.get("claims") or []:
        lines.append(f"  DOES NOT CLAIM: {claim}")
    lines.append(f"verdict: {report.get('verdict')}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: ``python -m evals.memory_ablation``.

    Exit 2 when the report has errors, matching the rest of ``evals``.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="the P3a long-term-memory ablation arm"
    )
    parser.add_argument("--arms", default=None, help="comma list of arms")
    parser.add_argument("--k", type=int, default=5, help="recall cutoff")
    parser.add_argument("--json", action="store_true", help="JSON only")
    args = parser.parse_args(list(argv) if argv is not None else None)
    arms = args.arms.split(",") if args.arms else None
    report = memory_ablation_report(arms=arms, k=args.k)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 0 if report.get("verdict") == "CLEAN" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
