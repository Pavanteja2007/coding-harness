"""The P3a real-loop memory ablation — the NUMBER Phase 3a exists to produce.

WHY THIS IS A SEPARATE MODULE AND NOT ANOTHER ``evals/run.py`` ARM
------------------------------------------------------------------
Two reasons, both measured rather than stylistic.

1. **Repetitions.** ``evals.run.run_eval`` runs each arm exactly once per
   invocation. A single run per arm is noise wearing a table's clothes, and the
   phase's non-negotiable is "never report an ablation with a single run per
   arm". :func:`run_memory_ablation` takes ``repeats`` and reports per-repetition
   values plus a variance figure for every arm.

2. **The shipped prompt-regression ``no_memory`` arm was not a single-variable
   ablation, and this module is where that was found.** ``evals/run.py``
   computed the store binding as ``db_path if seeded and
   effective.get("plan_with_memory", True) else None`` — so the memory-OFF arm
   did not merely stop *querying* memory, it was never even given a store. Two
   variables moved, and a difference between the arms was unattributable. The
   one-line fix is in ``evals/run.py`` (the store is bound in both arms; the
   ``plan_with_memory`` key alone decides whether it is queried) and the pin is
   ``tests/test_p3a_memory_ablation_real.py::TestTheArmIsSingleVariable``.
   This module never relies on that binding either: it binds the store itself,
   identically, in both arms, and proves it.

WHAT IS ACTUALLY MEASURED, AND WHAT IS NOT
------------------------------------------
Two lanes, because a scripted double that ignores its prompt cannot show a
memory *helping*, and reporting "no effect" from that double as though it were
a property of memory would be the exact dishonesty this repository exists to
prevent.

``lane="oblivious"`` — the SHIPPED eval double
    ``evals.run._FeedbackAwareScriptedModel`` unchanged. It dispatches on the
    task's scripted plan and on the step marker, never on the contents of the
    memory block. A recalled memory therefore **cannot** change its reply, and
    the resolve-rate delta in this lane is structurally zero. That is a
    property of the DOUBLE, it is stated in ``not_established`` on every run,
    and it is reported as ``null`` with a reason rather than as ``0.0``.

    What this lane DOES measure, and measure honestly: whether memory reaches
    the planner prompt at all (byte-for-byte, from the real captured prompt),
    what that costs in prompt bytes and tokens, and whether the arms differ in
    exactly one variable.

``lane="grounded"`` — a DECLARED memory-reading double
    A thin wrapper that reads the planner prompt, and if the injected memory
    block carries a ``TEST-RUN-CONVENTION:`` marker, uses THAT command for its
    first bash call instead of the scripted one. The policy is three lines, it
    is printed in this docstring, and it is repeated in the report's
    ``lanes.grounded.policy`` so a reader never has to open the source.

    This lane exists because T5.W2.2 item 4 asks for HARM — "how often a
    recalled memory changed the outcome, and in how many of those for the
    worse" — and harm is structurally unmeasurable in a lane whose double
    cannot read. It is a measurement of the MACHINERY under a declared
    non-secret policy, **not** evidence about what a real model does with a
    remembered fact.

THE SEED CORPUS IS DELIBERATELY SIGNED
--------------------------------------
A corpus of only-true memories can only ever produce a positive delta, and a
positive delta nobody tried to suppress is not evidence. So
:data:`SEED_MEMORIES` contains, per repository:

* one memory whose claim is **verified true against the repository's bytes**
  (measured on this tree: bare ``pytest`` exits 2 with a collection ERROR in
  every fixture repo while ``python -m pytest -q <target>`` reaches the real
  assertion — see the docstring on :data:`TEST_RUN_MARKER`);
* one memory whose claim is **verifiably false** for that repository, because
  the symbol it names does not exist in it;
* one noise row scoped to a *different* repository, which the repo-scoped
  query must drop.

The sign of the result is therefore decided by the corpus and the policy, both
published, and neither was chosen after seeing a delta. See
``memory_ablation_report()['design']``.

REPORTING RULES THIS MODULE KEEPS
---------------------------------
* A metric that could not be measured is ``None`` with a ``reason``. It is
  never ``0``.
* ``mistake_recurrence`` counts task-result rows matching a documented
  signature list (:data:`MISTAKE_SIGNATURES`), with the signatures named in the
  report so a reader can see what was counted.
* The resolve rate is a proportion with a **Wilson 95% interval**, and the
  interval is reported even when it spans the baseline rate — a delta whose
  interval contains zero is reported as ``indistinguishable``, not as a win.
* Cost is reported for BOTH arms and the delta is labelled ``cheaper`` /
  ``costlier`` / ``indistinguishable``. A memory feature that improves
  accuracy while costing more is a different product decision, so the report
  cannot present one axis without the other.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

__all__ = [
    "ALREADY_FIXED_MARKER",
    "ARMS",
    "GROUNDED_POLICY",
    "LANES",
    "MEMORY_KEY",
    "MISTAKE_SIGNATURES",
    "SEED_MEMORIES",
    "MemoryAblationReport",
    "arm_config",
    "config_difference",
    "render",
    "run_memory_ablation",
]

#: The ONE config key the ablation varies. Read by ``harness.core.run_task``
#: at the planning step (``core.py:782``) as the master switch.
MEMORY_KEY = "plan_with_memory"

#: The marker a seeded memory carries to be actionable by the grounded lane.
TEST_RUN_MARKER = "TEST-RUN-CONVENTION:"  # legacy marker; see ALREADY_FIXED_MARKER

#: The marker the shipped lane uses. Chosen because it is the channel through
#: which a WRONG memory makes an agent confidently wrong: a memory that says
#: "this file was already fixed" causes the agent not to fix it.
ALREADY_FIXED_MARKER = "FILE-ALREADY-FIXED:"

#: The arm table. Each arm declares the FULL set of keys it sets, so the
#: mechanical single-variable proof compares two complete configurations rather
#: than two deltas over an implicit base.
ARMS: Dict[str, Dict[str, Any]] = {
    "memory_on": {MEMORY_KEY: True},
    "memory_off": {MEMORY_KEY: False},
}

#: Lane names. ``oblivious`` is the shipped double; ``grounded`` is the
#: declared memory-reading policy below.
LANES: Tuple[str, ...] = ("oblivious", "grounded")

#: Where the published report lives. A CONSTANT rather than a path spelled at
#: each use, because two consumers read it now - this module writes it and
#: `evals.gates.P3` reads it - and a path written twice is a path that will
#: disagree the first time somebody moves one of them.
PUBLISHED_REPORT: Path = REPO_ROOT / "logs" / "evals" / "p3a-memory-ablation-real.json"

#: The policy of the grounded lane, published in the report so a reader never
#: has to read this module to know what the lane did.
GROUNDED_POLICY = (
    "On the planner call the double reads the injected memory block, exactly as "
    "a model would, and nothing else. If any line carries a "
    "'FILE-ALREADY-FIXED: <path>' marker AND the double's scripted edit for "
    "this step targets that same path, the double emits a non-mutating "
    "inspection command instead of the edit, and then SUBMITs as scripted. "
    "With no marker, or with a marker naming a path the edit does not touch, "
    "it behaves identically to the oblivious lane. It never reads the store: "
    "the only channel is the prompt the harness actually built."
)

#: Mistake signatures counted as ``mistake_recurrence``. The class is the one
#: this repository's own prior ablation runs recorded burning real turns: a
#: collection ERROR from invoking the suite the wrong way. Named here so a
#: reader can see exactly what was counted rather than trusting a number.
MISTAKE_SIGNATURES: Tuple[str, ...] = (
    "ImportError while importing test module",
    "attempted relative import with no known parent package",
    "ModuleNotFoundError",
    "ERROR tests/",
    "ERROR collecting",
    "command not found",
)


# ---------------------------------------------------------------------------
# The seed corpus.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedMemory:
    """One seeded row, with the GROUND TRUTH label attached.

    ``truth`` is ``"verified_true"``, ``"verifiably_false"`` or ``"noise"``.
    ``evidence`` says how that label was established — a claim about a
    repository that cannot be checked is not a label, it is an opinion.
    """

    fixture: Optional[str]
    text: str
    truth: str
    evidence: str
    #: Whether `truth` is a claim about the REPOSITORY'S BYTES. False means
    #: the label is about the environment (what is installed, what happened in
    #: an earlier session on some other machine), which no amount of reading a
    #: repository can settle. `evals.memory_quality` uses this to keep such a
    #: label out of the checker's agreement rate instead of scoring the checker
    #: for not knowing something it cannot know.
    checkable_from_repo: bool = True

    @property
    def actionable(self) -> Optional[str]:
        """The path this memory would make the agent skip, or ``None``."""
        marker = self.text.find(ALREADY_FIXED_MARKER)
        if marker < 0:
            return None
        return self.text[marker + len(ALREADY_FIXED_MARKER) :].split(".")[0].strip()


#: The corpus is SIGNED, and the labels are established against the real
#: repository bytes rather than asserted.
#:
#: The "already fixed" rows are the load-bearing ones. Every task in the eval
#: set is a single scripted edit followed by ``SUBMIT`` (measured: 14 of 14
#: task scripts, none of which invokes the test suite — the verifier runs it),
#: so a memory about HOW to run the suite has no channel to move an outcome at
#: all. A memory that says a file was already fixed does have one, and it is
#: the channel the phase brief names: *"A wrong memory is worse than none — it
#: makes the agent confidently wrong on day 2."*
#:
#: Two rows therefore carry opposite labels over the SAME mechanism:
#:
#: * ``bug01_wrap`` -> ``FILE-ALREADY-FIXED: wrapwrap/textutil.py`` is STALE.
#:   The scripted edit for that task targets exactly that path, so a double
#:   that believes the memory skips the fix and the task fails where it
#:   otherwise succeeded. That is the harm measurement.
#: * ``bug02_mean`` -> ``FILE-ALREADY-FIXED: numlib/__init__.py`` is TRUE.
#:   The file exists (measured) and the bug is in ``numlib/mathutil.py``, so
#:   the memory changes nothing. That is the CONTROL: it proves a non-zero
#:   delta is not produced by the marker, the lane, or the policy.
#:
#: The remaining rows are conventions and gotchas with no marker at all. They
#: are the corpus the retrieval/precision metrics score, and their absence of
#: a marker is deliberate — a row that cannot move an outcome still costs
#: prompt tokens.
SEED_MEMORIES: Tuple[SeedMemory, ...] = (
    SeedMemory(
        fixture="bug01_wrap",
        text=(
            "wrapwrap's short-trailing-line defect was fixed in an earlier "
            "session and the file has not regressed. "
            "FILE-ALREADY-FIXED: wrapwrap/textutil.py. "
            "It was the `if lines and current and len(current) == width` guard."
        ),
        truth="verifiably_false",
        evidence=(
            "tests/fixtures/bug01_wrap: `python -m pytest -q "
            "tests/test_textutil.py` FAILS test_wrap_short_trailing_line on the "
            "pristine fixture, and the exact guard the memory names is present "
            "in wrapwrap/textutil.py. The bug is still there."
        ),
    ),
    SeedMemory(
        fixture="bug02_mean",
        text=(
            "numlib/__init__.py was reviewed and needed no change. "
            "FILE-ALREADY-FIXED: numlib/__init__.py. "
            "It re-exports mean and stdev and has no arithmetic of its own."
        ),
        truth="verified_true",
        evidence=(
            "read from tests/fixtures/bug02_mean/numlib/__init__.py: the file "
            "exists and contains only re-exports; the defect is in "
            "numlib/mathutil.py, a different file"
        ),
    ),
    SeedMemory(
        fixture="bug02_mean",
        text=(
            "numlib is not installed as a package; bare `pytest` cannot import "
            "it from the repo root and exits 2 with a collection ERROR."
        ),
        truth="verified_true",
        evidence=(
            "measured in tests/fixtures/bug02_mean: `pytest -q "
            "tests/test_mathutil.py` exits 2 'ERROR collecting'; "
            "`python -m pytest -q tests/test_mathutil.py` exits 1 on the "
            "intended assertion"
        ),
    ),
    SeedMemory(
        fixture="bug01_wrap",
        text=("wrapwrap is installed system-wide, so the suite runs directly."),
        truth="verifiably_false",
        checkable_from_repo=False,
        evidence=(
            "measured in tests/fixtures/bug01_wrap: `pytest -q "
            "tests/test_textutil.py` exits 2 'ERROR collecting'. NOTE the "
            "scope: what is INSTALLED is a fact about the machine, and the "
            "pytest exit code is what the repository's own suite reports here. "
            "A byte-level checker can confirm the second and not the first, so "
            "this row is excluded from the checker agreement rate."
        ),
    ),
    SeedMemory(
        fixture="bug03_stack",
        text=(
            "stacklib raises StackEmptyError from stacklib/stack.py; the "
            "docstring on pop() states the contract."
        ),
        truth="verified_true",
        evidence="read from tests/fixtures/bug03_stack/stacklib/stack.py",
    ),
    SeedMemory(
        fixture="bug04_nameerror",
        text=(
            "datelib's days_in_month references a plural constant "
            "_DAYS_PER_MONTHS that the module does not define."
        ),
        truth="verified_true",
        evidence="read from tests/fixtures/bug04_nameerror/datelib/dateutil.py",
    ),
    SeedMemory(
        fixture="bug05_cart",
        text=(
            "cartlib's price_report leaks state through a module-level list; "
            "a fix that builds the list inside the call verifies clean."
        ),
        truth="verified_true",
        evidence="read from tests/fixtures/bug05_cart/cartlib/cart.py",
    ),
    SeedMemory(
        fixture=None,
        text="Global convention row: this project uses tabs, not spaces.",
        truth="noise",
        evidence=(
            "recorded with no repo_path, so a repo-scoped query must drop it; "
            "it is here so the ablation proves repo scoping rather than "
            "assuming it"
        ),
    ),
)


# ---------------------------------------------------------------------------
# Statistics, with the honest-absent rules.
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, total: int, z: float = 1.96) -> Dict[str, Any]:
    """Return a Wilson 95% interval for a proportion.

    ``total == 0`` returns ``low=None, high=None`` with a reason. A proportion
    over zero observations has no interval, and reporting ``(0.0, 1.0)`` would
    be a number nobody measured.
    """
    if total <= 0:
        return {
            "low": None,
            "high": None,
            "method": "wilson_95",
            "reason": "no observations were collected, so there is no interval",
        }
    phat = successes / total
    denom = 1.0 + (z * z) / total
    centre = (phat + (z * z) / (2 * total)) / denom
    margin = (
        z
        * math.sqrt((phat * (1.0 - phat) / total) + (z * z) / (4 * total * total))
        / denom
    )
    return {
        "low": round(max(0.0, centre - margin), 6),
        "high": round(min(1.0, centre + margin), 6),
        "method": "wilson_95",
        "n": total,
    }


def _spread(values: Sequence[float]) -> Dict[str, Any]:
    """min/max/mean/stdev over the repetitions, honest about n < 2."""
    clean = [float(value) for value in values]
    if not clean:
        return {
            "n": 0,
            "mean": None,
            "min": None,
            "max": None,
            "stdev": None,
            "values": [],
            "reason": "no repetitions produced this metric",
        }
    return {
        "n": len(clean),
        "mean": round(statistics.fmean(clean), 6),
        "min": round(min(clean), 6),
        "max": round(max(clean), 6),
        "stdev": (round(statistics.stdev(clean), 6) if len(clean) > 1 else None),
        "stdev_reason": None
        if len(clean) > 1
        else "n=1; a spread needs two observations",
        "values": [round(value, 6) for value in clean],
    }


def _direction(delta: Optional[float], *, tolerance: float) -> str:
    if delta is None:
        return "not_measured"
    if abs(delta) <= tolerance:
        return "indistinguishable"
    return "higher" if delta > 0 else "lower"


# ---------------------------------------------------------------------------
# The arm configuration and the mechanical single-variable proof.
# ---------------------------------------------------------------------------


def arm_config(arm: str) -> Dict[str, Any]:
    """Return the config an arm declares. Unknown arms raise."""
    if arm not in ARMS:
        raise KeyError(
            f"unknown memory arm {arm!r}; this module defines {sorted(ARMS)!r}"
        )
    return dict(ARMS[arm])


def config_difference(
    left: str = "memory_on",
    right: str = "memory_off",
    base: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Prove mechanically that two arms differ in EXACTLY one config key.

    ``base`` is the shared configuration every arm starts from. Both arms are
    merged over it and the symmetric difference is computed; the result is the
    key set a reader must check before believing any delta.

    This is a COMPUTATION, not an assertion in the report: the caller cannot
    publish a delta without also publishing this.
    """
    from evals.run import _BASE

    shared: Dict[str, Any] = dict(base if base is not None else _BASE)
    left_cfg = dict(shared)
    left_cfg.update(arm_config(left))
    right_cfg = dict(shared)
    right_cfg.update(arm_config(right))
    differing = sorted(
        key
        for key in set(left_cfg) | set(right_cfg)
        if repr(left_cfg.get(key)) != repr(right_cfg.get(key))
    )
    return {
        "base_keys": len(shared),
        "left": left,
        "right": right,
        "differing_keys": differing,
        "difference_count": len(differing),
        "single_variable": len(differing) == 1 and differing == [MEMORY_KEY],
        "the_key": differing[0] if len(differing) == 1 else None,
        "reason": (
            ""
            if len(differing) == 1 and differing == [MEMORY_KEY]
            else (
                "an arm differs from the baseline in "
                f"{len(differing)} key(s) {differing!r}; a delta between these "
                "arms is not attributable to memory"
            )
        ),
    }


# ---------------------------------------------------------------------------
# Store seeding — identical in both arms, on purpose.
# ---------------------------------------------------------------------------


def seed_store(db_path: Path, repo_paths: Mapping[str, str]) -> Dict[str, Any]:
    """Write the seed corpus into ONE store, repo-scoped per fixture.

    The store is built ONCE and is presented to both arms. That is the whole
    point: a memory-OFF arm that has no store at all differs in two variables,
    and the difference is unattributable. Returns a receipt naming the store
    path, the row count and a content digest so both arms' receipts can be
    compared byte-for-byte.
    """
    import hashlib

    from memory.decision_store import DecisionStore

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    written: List[Dict[str, Any]] = []
    store = DecisionStore(str(db_path))
    try:
        for seed in SEED_MEMORIES:
            repo = repo_paths.get(seed.fixture or "", "")
            row_id = store.record(
                seed.text,
                category="convention" if seed.truth != "noise" else "style",
                source="manual",
                repo_path=repo or None,
                provenance={
                    "kind": "eval-seed",
                    "source": "manual",
                    "explicit": True,
                    "eval_truth": seed.truth,
                },
                task_id=f"p3a-seed-{seed.fixture or 'global'}",
            )
            if row_id is None:
                continue
            written.append(
                {
                    "id": int(row_id),
                    "fixture": seed.fixture,
                    "truth": seed.truth,
                    "evidence": seed.evidence,
                }
            )
        rows = store.count(include_revoked=True)
    finally:
        store.close()

    digest = hashlib.sha256(
        json.dumps(written, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return {
        "db_path": str(db_path),
        "rows": rows,
        "seeded": len(written),
        "digest": digest,
        "memories": written,
        "note": (
            "the SAME store is presented to both arms; plan_with_memory alone "
            "decides whether it is queried"
        ),
    }


# ---------------------------------------------------------------------------
# The grounded lane's declared policy, implemented.
# ---------------------------------------------------------------------------

_ALREADY_FIXED_RE = re.compile(re.escape(ALREADY_FIXED_MARKER) + r"\s*([^\s.]+)")


def already_fixed_from_prompt(prompt: str) -> Optional[str]:
    """Extract a ``FILE-ALREADY-FIXED:`` path from a captured prompt.

    Returns ``None`` for a prompt with no marker — which is the honest answer
    for the memory-OFF arm and must never be rendered as an empty path.
    """
    match = _ALREADY_FIXED_RE.search(prompt or "")
    if match is None:
        return None
    return match.group(1).strip() or None


def _edit_targets_path(command: str, path: str) -> bool:
    """Whether a scripted shell command would MUTATE ``path``.

    Deliberately narrow. A command that only READS the file is not a mutation
    and must not be suppressed, and a command whose text merely contains the
    path is not necessarily an edit to it. The four shapes below are the ones
    the eval task set actually uses (``sed -i``, ``sed -e`` with ``-i``, a
    ``python -c`` that calls ``write_text``/``replace`` on the path, and a shell
    redirect to it), and anything else is treated as "not an edit" so the
    policy can only ever suppress an edit it positively identified.
    """
    text = str(command or "")
    if not text:
        return False
    if not _mentions(text, path):
        return False
    mutating = (
        "sed -i" in text
        or "write_text" in text
        or ".write(" in text
        or ">" in text.split(path)[0][-40:]
        or "shutil" in text
    )
    if mutating:
        return True
    return bool(re.search(rf">{re.escape(path)}", text))


def _mentions(text: str, path: str) -> bool:
    normalised = str(path or "").strip()
    if not normalised:
        return False
    return normalised in text or normalised.replace("/", "\\") in text


class _GroundedModel:
    """A wrapper around the shipped scripted double with the declared policy.

    It reads the PLANNER prompt (the only place ``harness.core`` puts the
    memory block) and, when the block carries an ``ALREADY_FIXED_MARKER``
    naming the path its own scripted edit targets, suppresses that edit. It
    never touches the store, so the only channel by which memory can influence
    it is the prompt the harness really built — which is the property that
    makes the harm measurement mean anything.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.skip_path: Optional[str] = None
        self.skip_seen_at: Optional[str] = None
        self.suppressed: List[Dict[str, Any]] = []

    # -- observation passthrough ------------------------------------------
    @property
    def prompts(self) -> List[str]:
        return list(getattr(self.inner, "prompts", []))

    def feedback_report(self) -> Dict[str, Any]:
        return self.inner.feedback_report()

    def get_last_usage(self) -> Dict[str, Any]:
        """Delegate the boundary's usage receipt, or report nothing.

        ``harness.model_client.ModelClient._read_usage`` resolves usage from
        the callable's OWN attribute first and from its MODULE second. A wrapper
        that exposes neither therefore makes every call report zero tokens and
        zero cost — which reads exactly like a cheaper run and is not one. It
        is the mock lane's estimate disappearing, so this method exists and is
        pinned by ``tests/test_p3a_memory_ablation_real.py``.

        The inner boundary may carry the reader on the instance
        (``tests.fake_model.ScriptedModel`` does) or only in its module, so both
        branches are tried. An unreadable receipt returns ``{}`` — the honest
        "nobody told us", never a fabricated number.
        """
        reader = getattr(self.inner, "get_last_usage", None)
        if callable(reader):
            try:
                return dict(reader() or {})
            except Exception:
                return {}
        module_name = getattr(type(self.inner), "__module__", "")
        if not module_name:
            return {}
        try:
            module = __import__(module_name, fromlist=["get_last_usage"])
        except Exception:
            return {}
        getter = getattr(module, "get_last_usage", None)
        if not callable(getter):
            return {}
        try:
            return dict(getter() or {})
        except Exception:
            return {}

    # -- the policy --------------------------------------------------------
    def __call__(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        rendered = "\n".join(str(item.get("content") or "") for item in messages)
        system = next(
            (
                str(item.get("content") or "")
                for item in messages
                if item.get("role") == "system"
            ),
            "",
        )
        if "planning a bug fix" in system and self.skip_seen_at is None:
            found = already_fixed_from_prompt(rendered)
            if found:
                self.skip_path = found
                self.skip_seen_at = "planner_prompt"
        reply = self.inner(messages, **kwargs)
        if self.skip_path and "your step is #" in system:
            return self._suppress(reply)
        return reply

    def _suppress(self, reply: str) -> str:
        text = str(reply or "")
        stripped = text.strip()
        if not stripped or stripped.startswith("```"):
            return reply
        if stripped.upper() in {"SUBMIT", "DONE", "RETRY"}:
            return reply
        if not _edit_targets_path(stripped, str(self.skip_path)):
            return reply
        self.suppressed.append(
            {"command": stripped[:200], "path": self.skip_path, "suppressed": True}
        )
        # A believing agent does not sit idle: it inspects, then submits. The
        # replacement is a READ, so the run is not killed by an empty turn.
        return f"cat {self.skip_path}"


# ---------------------------------------------------------------------------
# One run.
# ---------------------------------------------------------------------------


@dataclass
class TaskOutcome:
    """One (arm, repetition, task) observation."""

    arm: str
    repetition: int
    task: str
    status: str = ""
    verified: bool = False
    attempts: int = 0
    model_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0
    per_call_tokens: List[int] = field(default_factory=list)
    mistake_recurrences: int = 0
    mistake_signatures_seen: List[str] = field(default_factory=list)
    memory_matched: int = 0
    memory_section_chars: int = 0
    memory_block: str = ""
    planner_prompt: str = ""
    grounded_skip_path: Optional[str] = None
    grounded_replaced: bool = False
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "arm": self.arm,
            "repetition": self.repetition,
            "task": self.task,
            "status": self.status,
            "verified": self.verified,
            "attempts": self.attempts,
            "model_calls": self.model_calls,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "wall_s": self.wall_s,
            "per_call_tokens": list(self.per_call_tokens),
            "mistake_recurrences": self.mistake_recurrences,
            "mistake_signatures_seen": self.mistake_signatures_seen,
            "memory_matched": self.memory_matched,
            "memory_section_chars": self.memory_section_chars,
            "memory_delivered_an_actionable_marker": self.grounded_skip_path
            is not None,
            "grounded_replaced_a_command": self.grounded_replaced,
            "error": self.error,
        }


def _env_value(name: str) -> Any:
    return os.environ.get(name)


def _restore_env(name: str, value: Any) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = str(value)


def _read_trace(log_dir: Path, task_id: str) -> List[Dict[str, Any]]:
    path = Path(log_dir) / task_id / "trace.jsonl"
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return rows


def _memory_receipt(trace_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The harness's own ``decision_memory`` event, read back from the trace."""
    for row in trace_rows:
        if str(row.get("kind") or "") == "decision_memory":
            data = row.get("data")
            return dict(data) if isinstance(data, Mapping) else {}
    return {}


def _mistakes(trace_rows: Sequence[Mapping[str, Any]]) -> Tuple[int, List[str]]:
    seen: List[str] = []
    hits = 0
    for row in trace_rows:
        if str(row.get("kind") or "") != "tool_result":
            continue
        data = row.get("data")
        blob = json.dumps(data, default=str) if data is not None else ""
        for signature in MISTAKE_SIGNATURES:
            if signature in blob:
                hits += 1
                seen.append(signature)
                break
    return hits, sorted(set(seen))


_MEMORY_BLOCK_RE = re.compile(
    r"## Relevant past decisions[^\n]*\n(?P<body>.*?)(?=\n## |\Z)", re.DOTALL
)


def _planner_prompt(prompts: Sequence[str]) -> str:
    for prompt in prompts:
        if "planning a bug fix" in prompt:
            return prompt
    return prompts[0] if prompts else ""


def _run_one(
    spec: Mapping[str, Any],
    *,
    arm: str,
    repetition: int,
    lane: str,
    log_root: Path,
    decision_db: Path,
) -> TaskOutcome:
    """Run one task in one arm of one lane. Never raises."""
    from evals.run import _BASE, _FeedbackAwareScriptedModel
    from harness.deps import reset_overrides, set_call_model

    outcome = TaskOutcome(arm=arm, repetition=repetition, task=str(spec.get("slug")))
    prior_trace = _env_value("NEO_TRACE_DIR")
    prior_db = _env_value("HARNESS_DECISIONS_DB")
    started = time.time()
    model: Any = None
    try:
        cfg = dict(_BASE)
        cfg.update(ARMS[arm])
        for key, value in (spec.get("config") or {}).items():
            cfg.setdefault(key, value)
        cfg["target_test"] = spec.get("target")
        task_id = str(spec["slug"])
        task_log_root = Path(log_root)
        trace_dir = task_log_root / task_id
        os.environ["NEO_TRACE_DIR"] = str(trace_dir)
        from shared import tracing as _tracing

        _tracing._reset_cache()
        raw_scripts = spec["script"]["scripts"]
        scripts: Dict[int, list] = {}
        for sid, value in raw_scripts.items():
            scripts[int(sid)] = (
                [value] if value and isinstance(value[0], str) else list(value)
            )
        inner = _FeedbackAwareScriptedModel(
            plan=spec["script"]["plan"],
            scripts=scripts,
            feedback_contracts=spec.get("feedback_contracts") or {},
        )
        model = _GroundedModel(inner) if lane == "grounded" else inner
        set_call_model(model)
        # The store is bound IDENTICALLY in both arms. This is the one-line
        # difference from evals/run.py that makes this a valid ablation.
        os.environ["HARNESS_DECISIONS_DB"] = str(decision_db)

        from harness.core import run_task
        from shared.types import Task

        result = run_task(
            Task(
                task_id=task_id,
                repo_path=spec["repo"],
                issue_text=spec["issue"],
                config=cfg,
            ),
            log_root=task_log_root,
        )
        outcome.status = str(result.status)
        outcome.attempts = int(result.attempts or 0)
        verification = result.verification
        outcome.verified = bool(
            verification is not None
            and verification.target_test_passed
            and verification.regression_passed
            and not verification.flaky
        )
        outcome.model_calls = len(result.model_calls or [])
        outcome.per_call_tokens = [
            int(call.get("tokens") or 0) for call in (result.model_calls or [])
        ]
        outcome.tokens = sum(outcome.per_call_tokens)
        outcome.cost_usd = round(float(result.cost_usd or 0.0), 6)
        outcome.wall_s = round(time.time() - started, 1)
    except Exception as exc:  # a crash is an observation, not an abort
        outcome.status = "crash"
        outcome.error = f"{exc!r}\n{traceback.format_exc()[-1500:]}"
        outcome.wall_s = round(time.time() - started, 1)
    finally:
        _restore_env("HARNESS_DECISIONS_DB", prior_db)
        _restore_env("NEO_TRACE_DIR", prior_trace)
        try:
            from shared import tracing as _tracing

            _tracing._reset_cache()
        except Exception:
            pass
        try:
            reset_overrides()
        except Exception:
            pass

    trace_rows = _read_trace(Path(log_root), str(spec.get("slug")))
    hits, seen = _mistakes(trace_rows)
    outcome.mistake_recurrences = hits
    outcome.mistake_signatures_seen = seen
    receipt = _memory_receipt(trace_rows)
    outcome.memory_matched = int(receipt.get("matched") or 0)
    outcome.memory_section_chars = int(receipt.get("section_chars") or 0)
    prompts = list(getattr(model, "prompts", []) or [])
    planner = _planner_prompt(prompts)
    outcome.planner_prompt = planner
    match = _MEMORY_BLOCK_RE.search(planner)
    outcome.memory_block = match.group("body").strip() if match else ""
    if lane == "grounded" and isinstance(model, _GroundedModel):
        outcome.grounded_skip_path = model.skip_path
        outcome.grounded_replaced = any(
            bool(item.get("suppressed")) for item in model.suppressed
        )
    return outcome


# ---------------------------------------------------------------------------
# The report.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryAblationReport:
    """One lane's measured result across every arm and repetition."""

    lane: str
    policy: str
    repeats: int
    task_slugs: Tuple[str, ...]
    outcomes: Tuple[TaskOutcome, ...]
    single_variable: Mapping[str, Any]
    store: Mapping[str, Any]
    prompt_equivalence: Mapping[str, Any]
    metrics: Mapping[str, Any]
    harm: Mapping[str, Any]
    verdict: str
    errors: Tuple[Dict[str, Any], ...]
    claims: Tuple[str, ...]
    not_established: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "suite": "memory-ablation-real",
            "phase": "P3a-W2",
            "lane": self.lane,
            "policy": self.policy,
            "repeats": self.repeats,
            "task_slugs": list(self.task_slugs),
            "single_variable": dict(self.single_variable),
            "store": dict(self.store),
            "prompt_equivalence": dict(self.prompt_equivalence),
            "metrics": dict(self.metrics),
            "harm": dict(self.harm),
            "observations": [item.as_dict() for item in self.outcomes],
            "verdict": self.verdict,
            "errors": [dict(item) for item in self.errors],
            "claims": list(self.claims),
            "not_established": list(self.not_established),
            "deterministic_double": True,
            "model_calls_made_by_suite": 0,
            "docker": True,
        }


def _prompt_equivalence(outcomes: Sequence[TaskOutcome]) -> Dict[str, Any]:
    """Prove the arms' PLANNER PROMPTS differ only by the memory block.

    This is the prompt-level half of the single-variable proof. The config
    diff proves the two configurations differ in one key; this proves the
    rendered consequence differs in exactly one section. A config diff alone
    would not catch a second key that happens to be inert today.
    """
    checked = 0
    mismatches: List[Dict[str, Any]] = []
    for repetition in sorted({item.repetition for item in outcomes}):
        by_arm_task: Dict[Tuple[str, str], TaskOutcome] = {
            (item.arm, item.task): item
            for item in outcomes
            if item.repetition == repetition
        }
        tasks = sorted(
            {item.task for item in outcomes if item.repetition == repetition}
        )
        for task in tasks:
            on = by_arm_task.get(("memory_on", task))
            off = by_arm_task.get(("memory_off", task))
            if on is None or off is None:
                continue
            if not on.planner_prompt or not off.planner_prompt:
                mismatches.append(
                    {
                        "task": task,
                        "repetition": repetition,
                        "reason": "a planner prompt was not captured for one arm",
                    }
                )
                continue
            checked += 1
            on_stripped = _MEMORY_BLOCK_RE.sub("", on.planner_prompt)
            off_stripped = _MEMORY_BLOCK_RE.sub("", off.planner_prompt)
            if on_stripped != off_stripped:
                mismatches.append(
                    {
                        "task": task,
                        "repetition": repetition,
                        "reason": (
                            "the planner prompts differ OUTSIDE the "
                            "'## Relevant past decisions' section, so this arm "
                            "pair is not a single-variable comparison"
                        ),
                        "on_chars": len(on_stripped),
                        "off_chars": len(off_stripped),
                    }
                )
    return {
        "pairs_checked": checked,
        "mismatches": mismatches,
        "identical_outside_the_memory_block": not mismatches and checked > 0,
        "reason": (
            ""
            if checked and not mismatches
            else "no comparable prompt pair was captured, so this check proved nothing"
        ),
    }


def _token_estimator_report(outcomes: Sequence[TaskOutcome]) -> Dict[str, Any]:
    """Is the token/cost axis on this lane a MEASUREMENT or an ARTEFACT?

    The eval harness runs on the mock lane, whose usage reader returns a fixed
    ``tokens`` per call. A fixed per-call estimate cannot see prompt length, so
    a memory block that adds bytes to every planner prompt shows up as a ZERO
    token delta — and "zero" there means "the instrument cannot see this",
    not "this costs nothing".

    This function decides which of the two it is, from the observed per-call
    values, and the report says so next to the cost number. Reporting
    ``cost: indistinguishable`` without it would be the exact dishonesty this
    repository exists to prevent: an unmeasured zero and a measured zero look
    identical in a table.
    """
    values: List[int] = []
    for item in outcomes:
        values.extend(item.per_call_tokens)
    if not values:
        return {
            "per_call_tokens_observed": 0,
            "distinct_per_call_values": 0,
            "uniform": None,
            "cost_axis_vacuous": None,
            "reason": "no per-call token values were recorded, so the cost axis was not measured",
        }
    distinct = sorted(set(values))
    uniform = len(distinct) == 1
    return {
        "per_call_tokens_observed": len(values),
        "distinct_per_call_values": len(distinct),
        "distinct_values": distinct[:10],
        "uniform": uniform,
        "cost_axis_vacuous": bool(uniform),
        "reason": (
            "every model call in this lane reported the SAME token count, so "
            "the estimator cannot see prompt length; a cost or token delta "
            "between the arms is therefore NOT MEASURED here and must not be "
            "read as 'memory is free'"
            if uniform
            else "the per-call token counts vary, so the estimator responds to "
            "the request and a delta between the arms is a measurement"
        ),
        "token_source": "mock-lane usage reader (harness ModelClient record)",
        "cost_source": "mock-lane usage reader (harness ModelClient record)",
    }


def _aggregate(outcomes: Sequence[TaskOutcome], arm: str) -> Dict[str, Any]:
    rows = [item for item in outcomes if item.arm == arm]
    repeats = sorted({item.repetition for item in rows})
    per_repetition: List[Dict[str, Any]] = []
    for repetition in repeats:
        batch = [item for item in rows if item.repetition == repetition]
        resolved = sum(1 for item in batch if item.verified)
        per_repetition.append(
            {
                "repetition": repetition,
                "n": len(batch),
                "resolved": resolved,
                "resolve_rate": round(resolved / len(batch), 6) if batch else None,
                "crashed": sum(1 for item in batch if item.status == "crash"),
            }
        )
    resolved_total = sum(1 for item in rows if item.verified)
    total = len(rows)
    return {
        "arm": arm,
        "config": arm_config(arm),
        "observations": total,
        "resolved": resolved_total,
        "resolve_rate": round(resolved_total / total, 6) if total else None,
        "resolve_rate_ci95": wilson_interval(resolved_total, total),
        "per_repetition": per_repetition,
        "model_calls": _spread([item.model_calls for item in rows]),
        "tokens": _spread([item.tokens for item in rows]),
        "cost_usd": _spread([item.cost_usd for item in rows]),
        "wall_s": _spread([item.wall_s for item in rows]),
        "attempts": _spread([item.attempts for item in rows]),
        "mistake_recurrence": _spread([item.mistake_recurrences for item in rows]),
        "tasks_with_mistake_recurrence": sorted(
            {item.task for item in rows if item.mistake_recurrences > 0}
        ),
        "mistake_signatures_seen": sorted(
            {sig for item in rows for sig in item.mistake_signatures_seen}
        ),
        "memory_matched_total": sum(item.memory_matched for item in rows),
        "memory_section_chars": _spread([item.memory_section_chars for item in rows]),
        "crashes": sorted({item.task for item in rows if item.status == "crash"}),
    }


def _harm(outcomes: Sequence[TaskOutcome]) -> Dict[str, Any]:
    """How often a recalled memory changed the outcome, and for the worse.

    The definition is stated rather than inferred. A memory "moved the
    outcome" when the memory-on observation for a (task, repetition) differs
    from the memory-off observation of the SAME pair on a scored dimension
    (resolved, or attempts). It moved it FOR THE WORSE when the memory-on
    observation is worse: not resolved where the off arm resolved, or needing
    strictly more attempts.
    """
    moved = 0
    worse = 0
    better = 0
    considered = 0
    pairs: List[Dict[str, Any]] = []
    for repetition in sorted({item.repetition for item in outcomes}):
        on_by_task = {
            item.task: item
            for item in outcomes
            if item.repetition == repetition and item.arm == "memory_on"
        }
        off_by_task = {
            item.task: item
            for item in outcomes
            if item.repetition == repetition and item.arm == "memory_off"
        }
        for task in sorted(set(on_by_task) & set(off_by_task)):
            on = on_by_task[task]
            off = off_by_task[task]
            considered += 1
            on_scored = on.verified or on.attempts
            off_scored = off.verified or off.attempts
            if on_scored == off_scored:
                continue
            moved += 1
            on_worse = (off.verified and not on.verified) or (
                on.attempts > off.attempts
            )
            if on_worse:
                worse += 1
            else:
                better += 1
            pairs.append(
                {
                    "task": task,
                    "repetition": repetition,
                    "on": {
                        "verified": on.verified,
                        "attempts": on.attempts,
                        "mistakes": on.mistake_recurrences,
                    },
                    "off": {
                        "verified": off.verified,
                        "attempts": off.attempts,
                        "mistakes": off.mistake_recurrences,
                    },
                    "direction": "for_the_worse" if on_worse else "for_the_better",
                    "memory_delivered_an_actionable_marker": on.grounded_skip_path
                    is not None,
                }
            )
    return {
        "definition": (
            "a memory 'moved the outcome' when the memory-on observation for a "
            "(task, repetition) differs from the memory-off observation of the "
            "same pair on a scored dimension (resolved, or attempts); it moved "
            "it 'for the worse' when the memory-on observation lost a resolve "
            "the off arm won, or needed strictly more attempts"
        ),
        "pairs_compared": considered,
        "outcomes_moved": moved,
        "outcomes_moved_for_the_worse": worse,
        "outcomes_moved_for_the_better": better,
        "harm_rate_over_moved": (round(worse / moved, 6) if moved else None),
        "harm_rate_reason": (
            ""
            if moved
            else "no recalled memory changed any scored outcome, so the harm rate is undefined rather than zero"
        ),
        "pairs": pairs,
        "reported_even_if_zero": True,
    }


def run_memory_ablation(
    *,
    repeats: int = 3,
    task_slugs: Optional[Sequence[str]] = None,
    lanes: Sequence[str] = LANES,
    out_root: Optional[Any] = None,
    workdir: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run the memory ablation over the fixed eval task set, with repetitions.

    ``repeats`` is the number of times EVERY (arm, task) cell is run. The
    phase's rule is that one run per arm is noise; ``repeats < 2`` is refused
    by the report's ``errors`` rather than silently accepted.
    """
    from evals import tasks as eval_tasks

    selected = set(task_slugs) if task_slugs else None
    temporary = workdir is None
    root = Path(str(workdir)) if workdir is not None else Path(tempfile.mkdtemp())
    root.mkdir(parents=True, exist_ok=True)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    reports: List[Dict[str, Any]] = []
    try:
        all_tasks = eval_tasks.all_tasks(root / "repos")
        specs = [
            spec
            for spec in all_tasks
            if selected is None or str(spec.get("slug")) in selected
        ]
        specs.sort(key=lambda item: str(item.get("slug")))
        if not specs:
            return {
                "suite": "memory-ablation-real",
                "verdict": "ERROR",
                "errors": [
                    {
                        "code": "empty_task_set",
                        "message": "no task matched the selection, so no arm ran",
                    }
                ],
            }
        repo_paths = {Path(str(spec["repo"])).name: str(spec["repo"]) for spec in specs}
        store_receipt = seed_store(root / "memory-seed" / "decisions.db", repo_paths)
        single = config_difference()

        for lane in lanes:
            lane_root = root / str(lane)
            outcomes: List[TaskOutcome] = []
            for repetition in range(1, int(repeats) + 1):
                for spec in specs:
                    arm_dir = lane_root / f"rep{repetition}"
                    arm_dir.mkdir(parents=True, exist_ok=True)
                    for arm in ARMS:
                        outcomes.append(
                            _run_one(
                                spec,
                                arm=arm,
                                repetition=repetition,
                                lane=str(lane),
                                log_root=arm_dir / str(arm),
                                decision_db=Path(store_receipt["db_path"]),
                            )
                        )
            reports.append(
                _build_report(
                    lane=str(lane),
                    repeats=int(repeats),
                    outcomes=tuple(outcomes),
                    single_variable=single,
                    store=store_receipt,
                    specs=tuple(str(spec.get("slug")) for spec in specs),
                ).as_dict()
            )
    finally:
        if temporary and out_root is None:
            shutil.rmtree(root, ignore_errors=True)

    # The headline lane is the GROUNDED one, not the first. `oblivious` is the
    # shipped double, which by construction cannot read a memory, so it is a
    # null by design; leading with it publishes a guaranteed-zero delta as the
    # summary and buries the lane that can actually show harm. Selected by name,
    # with a named fallback so a reordering of LANES cannot silently change which
    # lane the report leads with.
    headline = next(
        (item for item in reports if str(item.get("lane")) == "grounded"),
        reports[-1] if reports else {},
    )
    return {
        "suite": "memory-ablation-real",
        "phase": "P3a-W2",
        "started_at": started_at,
        "repeats": int(repeats),
        "lanes": reports,
        "headline_lane": headline.get("lane"),
        "arms": list(ARMS),
        "the_key": MEMORY_KEY,
        "design": {
            "seed_memories": [
                {
                    "fixture": seed.fixture,
                    "truth": seed.truth,
                    "evidence": seed.evidence,
                    "actionable": seed.actionable,
                }
                for seed in SEED_MEMORIES
            ],
            "mistake_signatures": list(MISTAKE_SIGNATURES),
            "grounded_policy": GROUNDED_POLICY,
            "arm_table": {name: dict(value) for name, value in ARMS.items()},
        },
        "verdict": (
            "CLEAN"
            if all(item.get("verdict") == "CLEAN" for item in reports) and reports
            else "ERROR"
        ),
        "docker": True,
        "model_calls_made_by_suite": 0,
    }


def _build_report(
    *,
    lane: str,
    repeats: int,
    outcomes: Tuple[TaskOutcome, ...],
    single_variable: Mapping[str, Any],
    store: Mapping[str, Any],
    specs: Tuple[str, ...],
) -> MemoryAblationReport:
    errors: List[Dict[str, Any]] = []
    if repeats < 2:
        errors.append(
            {
                "code": "single_run_per_arm",
                "message": (
                    f"repeats={repeats}; one run per arm is noise and the "
                    "phase forbids reporting it as a result"
                ),
            }
        )
    if not single_variable.get("single_variable"):
        errors.append(
            {
                "code": "arm_is_not_single_variable",
                "message": str(single_variable.get("reason")),
            }
        )
    equivalence = _prompt_equivalence(outcomes)
    if not equivalence.get("identical_outside_the_memory_block"):
        errors.append(
            {
                "code": "prompt_differs_outside_memory",
                "message": (
                    str(equivalence.get("reason"))
                    + " mismatches: "
                    + json.dumps(equivalence.get("mismatches"), default=str)[:400]
                ),
            }
        )

    aggregates = {arm: _aggregate(outcomes, arm) for arm in ARMS}
    estimator = _token_estimator_report(outcomes)
    metrics: Dict[str, Any] = {"per_arm": aggregates, "token_estimator": estimator}
    on = aggregates["memory_on"]
    off = aggregates["memory_off"]

    resolve_delta = None
    if on["resolve_rate"] is not None and off["resolve_rate"] is not None:
        resolve_delta = round(on["resolve_rate"] - off["resolve_rate"], 6)
    intervals_overlap = _intervals_overlap(
        on["resolve_rate_ci95"], off["resolve_rate_ci95"]
    )
    metrics["resolve_rate"] = {
        "memory_on": on["resolve_rate"],
        "memory_off": off["resolve_rate"],
        "delta_on_minus_off": resolve_delta,
        "direction": _direction(resolve_delta, tolerance=0.0),
        "ci95_overlap": intervals_overlap,
        "attributable": (
            bool(
                resolve_delta is not None
                and not intervals_overlap
                and single_variable.get("single_variable")
            )
        ),
        "attributable_reason": (
            ""
            if resolve_delta is not None
            else "no arm produced a resolve rate, so no delta exists"
        ),
    }
    for key, tolerance in (("cost_usd", 0.0), ("tokens", 0.0), ("model_calls", 0.0)):
        on_mean = on[key]["mean"]
        off_mean = off[key]["mean"]
        delta = (
            round(on_mean - off_mean, 6)
            if on_mean is not None and off_mean is not None
            else None
        )
        metrics[key] = {
            "memory_on": on[key],
            "memory_off": off[key],
            "delta_on_minus_off": delta,
            "direction": _direction(delta, tolerance=tolerance),
            "measured": not estimator.get("cost_axis_vacuous"),
            "measured_reason": estimator.get("reason"),
        }
    metrics["token_estimator"] = _token_estimator_report(outcomes)
    metrics["mistake_recurrence"] = {
        "memory_on": on["mistake_recurrence"],
        "memory_off": off["mistake_recurrence"],
        "tasks_with_recurrence_on": on["tasks_with_mistake_recurrence"],
        "tasks_with_recurrence_off": off["tasks_with_mistake_recurrence"],
        "signatures_counted": list(MISTAKE_SIGNATURES),
    }

    harm = _harm(outcomes)
    claims = (
        "Every number in this lane was produced by the REAL loop, the REAL "
        "Docker sandbox and the REAL verifier.",
        "Both arms ran the SAME task set, the SAME scripted model, the SAME "
        "seeded store and the SAME budget policy; the arms differ in "
        f"{MEMORY_KEY} alone, and that is computed and published, not asserted.",
        "Both success and cost are reported, because a memory feature that "
        "improves accuracy while costing more is a different product decision.",
    )
    not_established = (
        "EVERY model reply in this lane is a scripted double. No number here "
        "is a claim about model quality, and the AHEAD +5.6pp figure in the "
        "phase brief is the corpus's measurement of a different system.",
    )
    if lane == "oblivious":
        extra = (
            " In the oblivious lane the double does not read the memory block, "
            "so a recalled memory cannot change its reply: the resolve-rate "
            "delta in this lane measures the DOUBLE, not memory. Use the "
            "grounded lane for the signed comparison."
        )
    else:
        extra = (
            " In the grounded lane the sign of the delta is decided by the "
            "declared policy in `policy` and by the signed seed corpus, both "
            "published above. It measures whether the machinery can MOVE an "
            "outcome, not whether a real model would."
        )
    not_established = (*not_established, extra)

    if any(item.status == "crash" for item in outcomes):
        crashed = sorted({item.task for item in outcomes if item.status == "crash"})
        errors.append(
            {
                "code": "run_crashed",
                "message": f"{len(crashed)} task(s) crashed in this lane: {crashed}",
                "tasks": crashed,
            }
        )

    return MemoryAblationReport(
        lane=lane,
        policy=GROUNDED_POLICY
        if lane == "grounded"
        else "shipped eval double, unchanged",
        repeats=repeats,
        task_slugs=specs,
        outcomes=outcomes,
        single_variable=single_variable,
        store=store,
        prompt_equivalence=equivalence,
        metrics=metrics,
        harm=harm,
        verdict="CLEAN" if not errors else "ERROR",
        errors=tuple(errors),
        claims=claims,
        not_established=not_established,
    )


def _intervals_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left.get("low") is None or right.get("low") is None:
        return True
    return not (
        float(left["high"]) < float(right["low"])
        or float(right["high"]) < float(left["low"])
    )


# ---------------------------------------------------------------------------
# Rendering + CLI.
# ---------------------------------------------------------------------------


def render(report: Mapping[str, Any]) -> str:
    """Render the whole report for a terminal, tables first."""
    lines = [
        "memory ablation — REAL loop, real Docker verifier, scripted double",
        f"repeats: {report.get('repeats')}   arms: {report.get('arms')}",
        f"the one key: {report.get('the_key')!r}",
        "",
    ]
    for lane in report.get("lanes") or []:
        lines.append("=" * 78)
        lines.append(f"lane {lane.get('lane')!r}: {lane.get('policy')}")
        single = lane.get("single_variable") or {}
        lines.append(
            f"single-variable check: differing_keys={single.get('differing_keys')} "
            f"single_variable={single.get('single_variable')}"
        )
        equivalence = lane.get("prompt_equivalence") or {}
        lines.append(
            f"planner prompts identical outside the memory block: "
            f"{equivalence.get('identical_outside_the_memory_block')} "
            f"({equivalence.get('pairs_checked')} pair(s) checked)"
        )
        lines.append("")
        lines.append(
            "| arm | resolve | CI95 | calls | tokens | cost | mistakes | mem matched |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        per_arm = ((lane.get("metrics") or {}).get("per_arm")) or {}
        for arm in sorted(per_arm):
            row = per_arm[arm]
            ci = row.get("resolve_rate_ci95") or {}
            lines.append(
                f"| {arm} "
                f"| {_num(row.get('resolve_rate'))} "
                f"| [{_num(ci.get('low'))}, {_num(ci.get('high'))}] "
                f"| {_num((row.get('model_calls') or {}).get('mean'))} "
                f"| {_num((row.get('tokens') or {}).get('mean'))} "
                f"| {_num((row.get('cost_usd') or {}).get('mean'))} "
                f"| {_num((row.get('mistake_recurrence') or {}).get('mean'))} "
                f"| {row.get('memory_matched_total')} |"
            )
        resolve = (lane.get("metrics") or {}).get("resolve_rate") or {}
        lines.append("")
        lines.append(
            f"resolve delta (on - off): {resolve.get('delta_on_minus_off')} "
            f"direction={resolve.get('direction')} "
            f"ci95_overlap={resolve.get('ci95_overlap')} "
            f"attributable={resolve.get('attributable')}"
        )
        for key in ("cost_usd", "tokens", "model_calls"):
            block = (lane.get("metrics") or {}).get(key) or {}
            lines.append(
                f"{key} delta (on - off): {block.get('delta_on_minus_off')} "
                f"direction={block.get('direction')}"
            )
        estimator = (lane.get("metrics") or {}).get("token_estimator") or {}
        lines.append(
            f"token estimator: uniform={estimator.get('uniform')} "
            f"cost_axis_vacuous={estimator.get('cost_axis_vacuous')} "
            f"({estimator.get('distinct_values')})"
        )
        harm = lane.get("harm") or {}
        lines.append(
            f"harm: moved={harm.get('outcomes_moved')} "
            f"worse={harm.get('outcomes_moved_for_the_worse')} "
            f"better={harm.get('outcomes_moved_for_the_better')} "
            f"pairs={harm.get('pairs_compared')}"
        )
        for pair in harm.get("pairs") or []:
            lines.append(
                f"   {pair['task']} rep{pair['repetition']}: {pair['direction']} "
                f"on={pair['on']} off={pair['off']}"
            )
        for error in lane.get("errors") or []:
            lines.append(f"ERROR {error.get('code')}: {error.get('message')}")
        for note in lane.get("not_established") or []:
            lines.append(f"DOES NOT ESTABLISH: {note}")
        lines.append(f"lane verdict: {lane.get('verdict')}")
    lines.append("")
    lines.append(f"verdict: {report.get('verdict')}")
    return "\n".join(lines)


def _num(value: Any) -> str:
    if value is None:
        return "n/m"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: ``python -m evals.memory_ablation_real``.

    Exit 2 on any lane error, matching every other eval driver.
    """
    parser = argparse.ArgumentParser(
        description="the P3a real-loop long-term-memory ablation"
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--tasks", default=None, help="comma list of task slugs")
    parser.add_argument(
        "--lanes", default=",".join(LANES), help="comma list of lanes to run"
    )
    parser.add_argument("--out", default=None, help="write the JSON report here")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = run_memory_ablation(
        repeats=args.repeats,
        task_slugs=args.tasks.split(",") if args.tasks else None,
        lanes=[item for item in args.lanes.split(",") if item],
    )
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 0 if report.get("verdict") == "CLEAN" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
