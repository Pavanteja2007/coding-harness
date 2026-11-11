"""The MULTI-SESSION evaluation: does the system still know what it was doing?

**Phase 4's core deliverable, and the honest answer to Anthropic's claim that the
initializer plus one-feature-per-session discipline "turned out to be critical."
We cannot reproduce their number. We can build the measurement that would show it
either way, and this module is that measurement.**

The premise it refuses: *a task that fits in one session measures nothing.* So
the first thing this module builds is a task that **genuinely cannot be finished
in one session**, and proves it with a control arm rather than asserting it. The
control is a single session given the SAME per-session turn budget as a session in
the multi-session run; if it finishes every feature, the task is wrong and the
whole evaluation is reported as ``task_fits_one_session`` rather than as a result.

Four arms, one variable each (see :data:`MULTI_SESSION_ARMS`):

===================  ==========================================================
arm                  what it isolates
===================  ==========================================================
``baseline``         multi-session, NO initializer, NO ledger discipline
``initializer``      ``init.sh`` + ``features.json`` + ``PROGRESS.md``
``read_before_edit`` tool-level enforcement on top of the initializer
``subagent_isolation``  isolated subagents on top of the initializer
===================  ==========================================================

**The arms are key-presence opt-ins, and that is a deliberate consequence of the
config rule**, not a convenience. ``evals/tasks.py::test_runner_arms_use_real_config_keys``
requires every key in the prompt-regression ``ARMS`` table to exist in
``harness.config.DEFAULTS`` -- and a value in ``DEFAULTS`` is merged into EVERY
task and EVERY eval arm, so publishing ``initializer_enabled: True`` there would
silently switch every run in the project onto a new architecture on the day the
key landed. ``evals/memory_ablation.py::MEMORY_ARMS`` is the existing precedent for
keeping an ablation's arms OUT of that table; this follows it, and
``OPT_IN_KEYS_ARE_NOT_DEFAULTS`` is pinned so the separation cannot quietly
regress.

**W2.1 changed the session body, because two of the four arms were VOID.**
The P4/W1 body wrote each feature's implementation directly from
:func:`_scripted_session`. Under that body the keys ``read_before_edit`` and
``subagent_isolation`` were read by nothing on the path a session actually took,
so those two arms could not have separated even if the measurement had been
right. **An arm whose mechanism never runs is not an arm** and
``waves`` ``Never run an ablation where one arm is void`` is the rule that
forbids it. The body is now :func:`_bounded_session`:

* every turn is a **real** ``harness.agent_kernel.tools.ToolRegistry.execute``
  call, so the read-before-edit gate is the product's gate and can genuinely
  refuse;
* ``session_turns`` is now **enforced** -- a session that reaches its budget
  ends, and every turn is counted;
* ``subagent_isolation`` moves the work into a bounded subagent with its OWN
  message list and returns only a compressed summary, which is what the
  ``parent_context_tokens`` metric then measures;
* the **verification gate is not a turn**, because a verifier is not a model
  turn. Charging it to the budget would have made the budget a fiction.

Session boundaries remain real in the only sense this runner can make them real,
and the limit of that claim is stated here rather than in a footnote. Every
session builds its first prompt through :func:`build_session_brief`, which reads
ONLY durable artefacts -- ``init.sh``, ``features.json``, ``PROGRESS.md``, the
request and the constraints -- and holds no message list.
``tests/test_multi_session_eval.py`` plants a marker that exists ONLY in session
1's conversation and asserts it is absent from session 3's brief. A long context
would carry it; this cannot. **That is the survival-vs-compaction distinction,
and it is genuinely under test.**

**What the boundary still is NOT:** the session body is a scripted, deterministic
policy -- it decides *what to attempt*, and every attempt goes through the real
product path. It is **not** ``harness.agent_loop.run_agent``, and the turn ceiling
is the harness's own, not the interactive loop's. So these numbers are about the
ARCHITECTURE and are **not** evidence about how a real model uses a ledger.
``MODEL_QUALITY_MEASURED`` is ``False`` on every receipt, and the nightly lane is
where a live provider would enter. A metric that cannot be measured is published
as ``null``, never as a favourable number.

Six metrics, every one of which may be ``None`` **with a reason**. A null result
is a finding (the arm's mechanism is not wired, the driver could not observe the
thing); a ``0`` is a measurement. The two are never the same value, which is
``phases/DOCTRINE.md`` section 1's rule applied to an eval rather than a receipt.

**Variance is published, not implied.** :meth:`MultiSessionReport.spread` reports
the observed min/median/max and the number of DISTINCT values per metric per arm,
so a reader can see at a glance whether a difference between two arms is larger
than the run-to-run noise inside one arm.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from harness import long_horizon as LH
from memory import feature_ledger_store as FL

#: The measurement is about the architecture, not about a model. On every
#: receipt, and asserted in the pins, so a reader cannot quote a token count as a
#: model-quality number.
MODEL_QUALITY_MEASURED = False

#: The four arms. Config-dict overrides on ``Task.config``, NOT code forks --
#: the same discipline as ``evals/run.py::ARMS``, for the same reason.
#:
#: ``baseline`` is deliberately EMPTY. An arm that sets nothing is the OFF arm by
#: key absence, which is the only shape in this repository whose absence really
#: means "unchanged behaviour".
MULTI_SESSION_ARMS: Dict[str, Dict[str, Any]] = {
    # multi-session, no initializer, no ledger discipline
    "baseline": {},
    # the initializer + the two ledgers
    "initializer": {
        "long_horizon_initialize": True,
        "long_horizon_ledgers": True,
        "long_horizon_one_feature_per_session": True,
    },
    # ... plus the tool-level read-before-edit gate. ONE key: the
    # same-turn half of that gate is OFF by key absence (its reader is
    # key-presence), so naming it here would be a second variable.
    "read_before_edit": {
        "long_horizon_initialize": True,
        "long_horizon_ledgers": True,
        "long_horizon_one_feature_per_session": True,
        "read_before_edit": True,
    },
    # ... plus isolated subagents
    "subagent_isolation": {
        "long_horizon_initialize": True,
        "long_horizon_ledgers": True,
        "long_horizon_one_feature_per_session": True,
        "subagent_isolation": True,
    },
}

#: Every key any arm sets, sorted. Published so a consumer can check what this
#: evaluation is actually toggling without reading four dicts.
OPT_IN_KEYS: Tuple[str, ...] = tuple(
    sorted({key for overrides in MULTI_SESSION_ARMS.values() for key in overrides})
)

#: The six metrics. A closed set, so a reader knows what was NOT measured.
METRIC_NAMES: Tuple[str, ...] = (
    "completion",
    "rework_rate",
    "constraint_retention",
    "parent_context_tokens",
    "tokens_per_session",
    "cost_usd",
)

#: The closed set of per-arm VERDICTS. ``null`` is a first-class member and is
#: the point of this module: an arm whose mechanism is not wired has produced no
#: finding, and reporting a number for it would be inventing one.
ARM_VERDICTS: Tuple[str, ...] = (
    "measured",
    "task_fits_one_session",
    "arm_mechanism_not_wired",
    "not_run",
)

#: The session-1 CONSTRAINT SET. This is what "constraint retention" measures: a
#: constraint stated once, in session 1, that must still govern session 3.
#:
#: Each entry is ``(constraint_id, forbidden_path_glob, why)``. The path shape is
#: chosen because it is CHECKABLE against the session's real changed set rather
#: than against what the model said about it -- a constraint measured by grepping
#: a transcript measures compliance narration, not compliance.
CONSTRAINTS: Tuple[Tuple[str, str, str], ...] = (
    ("no_test_edits", "tests/*", "Anthropic: removing or editing tests is unacceptable"),
    ("no_new_dependency", "requirements.txt", "no new dependency may be added mid-effort"),
    ("no_vcs_write", ".git/*", "the agent does not write VCS metadata"),
)

#: How many sessions the multi-session run is given, and the per-session turn
#: budget that makes one session insufficient. ``SESSIONS`` is also the minimum
#: for the evaluation to mean anything; the control arm proves the budget is tight.
#:
#: **FOUR, and the arithmetic is the whole design.** ``FEATURE_NAMES`` holds four
#: features and one session claims one, so a complete effort needs four sessions.
#: The per-session budget is :data:`DEFAULT_SESSION_TURNS` turns, so a SINGLE
#: bounded session can attempt at most three features and must leave at least one
#: open -- which is what the control arm observes with a real pytest run rather
#: than assuming. Three features would have made the control able to finish, which
#: is the failure this constant exists to prevent.
DEFAULT_SESSIONS = 4

#: **Three, and it is tight on purpose.** The scripted policy ATTEMPTS THE WRITE
#: FIRST on a file it has not read, because that is the defect class the
#: read-before-edit gate exists to catch. With the gate OFF that costs ONE turn
#: per feature. With the gate ON the same attempt is refused ``never_read``, the
#: session spends its next turn reading, and the write succeeds on the third --
#: exactly the budget. So the gate is measurable (it triples the turns) without
#: being engineered into a failure: a gate that could not complete the effort at
#: the shipped budget would be reporting a budget problem as a mechanism result.
#: :func:`control_arm_turn_arithmetic` publishes the arithmetic and the control
#: arm re-derives the consequence on every run.
DEFAULT_SESSION_TURNS = 3
DEFAULT_REPETITIONS = 3

#: Tokens per character for the ONE estimator this module uses. Named, and
#: published on every receipt: it is a heuristic, not a tokenizer, and a token
#: count that pretends to be exact is the same lie at a different scale.
CHARS_PER_TOKEN = 4.0

#: The multi-session task set. **TWO entries as of P4/W2.3, which CLOSES the
#: one-task gap pin.** P4/W1 published one and pinned the limitation as an
#: inverted pin. One task measures one shape of multi-session work, and the pin
#: said plainly that the limitation had to be addressed or stated.
#:
#: It is addressed. The two tasks differ in the SHAPE of the cross-session
#: obligation, not merely in the names of the modules:
#:
#: ``multi_session_shop``
#:     four INDEPENDENT features. A session that finishes its feature cannot
#:     affect another's. The question this task answers is *does a durable brief
#:     survive to a later session at all*.
#:
#: ``multi_session_cascade``
#:     four features in a DEPENDENCY CHAIN -- each one imports the previous
#:     module, so a session that reverts or rewrites an earlier module breaks a
#:     later one. The question this task answers is *does the durable ledger stop
#:     a later session from redoing earlier work*, which is the rework claim and
#:     the one the shop shape structurally cannot reach.
#:
#: A third shape -- a task whose constraints are only recoverable from the
#: progress ledger rather than the brief -- is NOT here, and
#: :data:`TASK_SHAPES_NOT_COVERED` names it so the coverage claim is a closed
#: set rather than a feeling.
TASK_SLUGS: Tuple[str, ...] = ("multi_session_shop", "multi_session_cascade")

#: The shapes a multi-session task can have that these TWO do NOT cover. Named,
#: so "two tasks" cannot be read as "multi-session work is covered".
TASK_SHAPES_NOT_COVERED: Tuple[str, ...] = (
    "constraints recoverable ONLY from the progress ledger, not from the brief",
    "a task where a later session must REVERT an earlier session's change",
    "a task whose sessions interleave on the same file (no one-feature-per-session)",
    "a real-model task, where a session might decline the work it was given",
)

#: The feature names the ``shop`` task is built from. FOUR entries so a caller
#: can build a LONGER task than the default session count -- and the control
#: then refuses, which is the mechanism that keeps "the task requires multiple
#: sessions" a measurement rather than a claim.
FEATURE_NAMES: Tuple[str, ...] = ("receipts", "pricing", "serializers", "reports")

#: The dependency chain's feature names, in order. Feature N's test imports
#: feature N-1's module, so the chain is real: a session that rewrites
#: ``coupons`` after ``taxes`` was finished can silently break ``taxes``.
CASCADE_NAMES: Tuple[str, ...] = ("coupons", "taxes", "basket", "invoice")

#: Which feature set belongs to which slug. DATA rather than a branch inside
#: :func:`build_task_repo`, so a reader can see the task table without reading
#: the builder, and a third task is a row here rather than an edit inside a
#: function.
TASK_FEATURE_NAMES: Dict[str, Tuple[str, ...]] = {
    "multi_session_shop": FEATURE_NAMES,
    "multi_session_cascade": CASCADE_NAMES,
}

#: The task run when a caller names none. Named rather than defaulted inline so
#: "which task is the default" has one answer in one place.
DEFAULT_TASK_SLUG: str = "multi_session_shop"


# ---------------------------------------------------------------------------
# The task: a feature that genuinely cannot be delivered in one session
# ---------------------------------------------------------------------------


def build_task_repo(root: Any, *, features: int = DEFAULT_SESSIONS, slug: str = "") -> Dict[str, Any]:
    """Build a real repository with ``features`` independent, individually-checkable features.

    Each feature is a separate module with its own test, so "one feature per
    session" has something to be true of, and a session that finishes one feature
    genuinely finishes it (its own test passes and nothing else's breaks).

    The default feature count is :data:`DEFAULT_SESSIONS`, which is what makes
    the task span exactly as many sessions as it has features: N sessions finish
    N of N, and the single-session control finishes fewer than N -- so the
    control cannot complete and the completion metric can actually discriminate.

    ``slug`` selects the task SHAPE. The two shapes differ in the dependency
    structure between features, which is the thing that decides what a later
    session can break:

    * ``multi_session_shop`` -- independent modules. Nothing a session does to
      one can break another, so the ledger's job is purely to SURVIVE.
    * ``multi_session_cascade`` -- feature N's module imports feature N-1's, so
      the tests genuinely depend on each other and a later session's rewrite can
      break an earlier feature's green. The ledger's job is to prevent REWORK,
      which is the claim the shop shape structurally cannot reach.

    Returns the task dict: ``slug``, ``repo``, ``request``, ``features``,
    ``target_all``, and ``shape``. Deliberately plain data -- the same shape
    ``evals/tasks.py`` uses, so a reader comparing the two does not have to
    learn two vocabularies.
    """
    base = Path(str(root))
    wanted = str(slug or DEFAULT_TASK_SLUG)
    if wanted not in TASK_FEATURE_NAMES:
        raise KeyError(
            f"unknown multi-session task {wanted!r}; known tasks are "
            f"{sorted(TASK_FEATURE_NAMES)}. Returning a default task would publish the "
            "baseline under the requested name, which is the dishonesty an arm table is "
            "exposed to."
        )
    cascade = wanted == "multi_session_cascade"
    package = base / ("chain" if cascade else "shop")
    tests = base / "tests"
    package.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)
    (base / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = \"\"\n", encoding="utf-8", newline="\n"
    )
    (package / "__init__.py").write_text("", encoding="utf-8", newline="\n")

    names = list(TASK_FEATURE_NAMES[wanted])[: max(1, min(int(features), 4))]
    for index, name in enumerate(names):
        imports = f"from {package.name}.{names[index - 1]} import {names[index - 1]}_value\n\n" if (cascade and index) else ""
        body = (
            f"{imports}\n"
            f"def {name}_value(items):\n"
            f'    """UNIMPLEMENTED: returns 0."""\n'
            "    return 0\n"
        )
        (package / f"{name}.py").write_text(body, encoding="utf-8", newline="\n")
        # Every feature asserts the SAME sum, in both task shapes: the cascade
        # differs in what a feature IMPORTS, not in what it must return, so the
        # assertion must not encode the shape.
        assertion = " == 3"
        (tests / f"test_{name}.py").write_text(
            f"from {package.name}.{name} import {name}_value\n\n\n"
            f"def test_{name}():\n    assert {name}_value([1, 2]){assertion}\n",
            encoding="utf-8",
            newline="\n",
        )
    return {
        "slug": wanted,
        "shape": "dependency_chain" if cascade else "independent_features",
        "package": package.name,
        "repo": str(base),
        "request": (
            "Implement every unimplemented function in "
            f"{package.name}/. Each returns 0 today and must return the sum of its argument "
            "list. Do not change tests."
        ),
        "features": list(names),
        "target_all": "tests",
        "source": "synthesized",
    }


def task_features(task: Mapping[str, Any]) -> List[LH.Feature]:
    """Return the ledger features for ``task``, each with its OWN runnable check.

    One check per feature, not one check for the whole task: a feature whose
    verification is the entire suite cannot be marked passing until every other
    feature is done, which would make "one feature per session" unmeasurable.
    """
    return [
        LH.new_feature(
            str(name),
            f"{task.get('package') or 'shop'}/{name}.py implements {name}_value as the sum of its "
            "argument list",
            LH.VerificationMethod(
                method="command", command=f"python -m pytest -q tests/test_{name}.py"
            ),
        )
        for name in (task.get("features") or ())
    ]


# ---------------------------------------------------------------------------
# The session boundary -- the load-bearing mechanism
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionBrief:
    """Everything a session is allowed to know at its FIRST model request.

    Four sources and no fifth: the request, the generated ``init.sh``, the
    ``features.json`` current view, and ``PROGRESS.md``. There is deliberately no
    field for a prior conversation, a prior summary, or a compaction -- because
    this evaluation measures whether a session survives on ARTEFACTS, and a
    field that could carry history would make the answer unmeasurable.
    """

    session_index: int
    request: str
    init_script: str
    progress_text: str
    feature_view: Tuple[Dict[str, str], ...]
    claimed_feature: str
    constraints: Tuple[Tuple[str, str, str], ...] = ()

    def render(self) -> str:
        """Render the brief, using T1's own session-start renderer for the block.

        Delegating the wording to ``harness.long_horizon.render_session_brief``
        means the evaluation measures the SAME discipline the product ships,
        rather than a paraphrase of it that could drift.
        """
        ledger = LH.new_feature_ledger(
            "multi-session", self.request, [_stub_feature(view) for view in self.feature_view]
        )
        # The REQUEST is rendered explicitly. T1's session-start block shows the
        # ledger and the progress ledger; the thing the session is actually being
        # asked to do belongs in front of it, and a brief that omitted it would
        # measure a session that had forgotten its own task.
        parts = ["## Request\n\n" + (self.request.strip() or "(no request recorded)"), LH.render_session_brief(
            ledger, self.progress_text, "", claimed_feature=self.claimed_feature
        )]
        if self.init_script:
            parts.append("## Generated init.sh (how to run this project)\n\n```bash\n" + self.init_script + "```")
        if self.constraints:
            rules = "\n".join(f"- {cid}: {why} ({pattern})" for cid, pattern, why in self.constraints)
            parts.append("## Constraints in force (stated in an earlier session; still binding)\n\n" + rules)
        return "\n\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection, including the rendered size."""
        rendered = self.render()
        return {
            "session_index": int(self.session_index),
            "claimed_feature": str(self.claimed_feature),
            "feature_view": [dict(view) for view in self.feature_view],
            "constraints": [list(item) for item in self.constraints],
            "init_script_chars": len(self.init_script),
            "progress_chars": len(self.progress_text),
            "brief_chars": len(rendered),
            "brief_tokens_heuristic": _tokens(len(rendered)),
            "sources": ["request", "init.sh", "features.json", "PROGRESS.md"],
            "carries_prior_conversation": False,
            "note": (
                "'carries_prior_conversation' is a CONSTANT, not a measurement: the "
                "brief has no field that could hold one, and the pin plants a marker "
                "in session 1's conversation to prove it."
            ),
        }


def _stub_feature(view: Mapping[str, str]) -> LH.Feature:
    """Return a throwaway feature carrying one line of the brief's current view.

    The brief renders the LEDGER STATUS TABLE, and it must render the real
    statuses. It does not need the real obligations to do that, and reusing T1's
    own renderer with a stub feature keeps the rendering path identical while
    making it obvious that the brief is a VIEW, not a second ledger.
    """
    return LH.new_feature(
        str(view.get("id") or ""),
        str(view.get("description") or ""),
        LH.VerificationMethod(method="command", command=str(view.get("command") or "")),
    )


def build_session_brief(
    task: Mapping[str, Any],
    root: Any,
    *,
    session_index: int,
    claimed_feature: str = "",
    constraints: Sequence[Tuple[str, str, str]] = CONSTRAINTS,
    initializer: bool = True,
) -> SessionBrief:
    """Build session ``session_index``'s brief from DURABLE ARTEFACTS only.

    The function's SIGNATURE is the claim: it takes a task and a project root,
    never a prior session, so there is no argument through which history could
    arrive. A caller that wanted to pass a conversation would have to build one
    out of text, and the plant in the pin suite would find it.
    """
    base = Path(str(root))
    view: List[Dict[str, str]] = []
    ledger_path = base / LH.FEATURE_LEDGER_NAME
    if ledger_path.is_file():
        ledger, refusal = LH.load_feature_ledger(ledger_path)
        if ledger is not None and refusal is None:
            for feature in ledger.features:
                view.append(
                    {
                        "id": feature.id,
                        "description": feature.description,
                        "status": feature.status,
                        "command": str(feature.verification.to_dict().get("command") or ""),
                    }
                )
        else:
            view.append(
                {
                    "id": "(ledger unreadable)",
                    "description": str(refusal.detail if refusal is not None else ""),
                    "status": "unknown",
                    "command": "",
                }
            )
    return SessionBrief(
        session_index=int(session_index),
        request=str(task.get("request") or ""),
        init_script=(
            (base / LH.INIT_SCRIPT_NAME).read_text(encoding="utf-8-sig", errors="replace")
            if initializer and (base / LH.INIT_SCRIPT_NAME).is_file()
            else ""
        ),
        progress_text=LH.load_progress_ledger(base / LH.PROGRESS_LEDGER_NAME) if initializer else "",
        feature_view=tuple(view),
        claimed_feature=str(claimed_feature),
        constraints=tuple(constraints) if initializer else (),
    )


# ---------------------------------------------------------------------------
# The runner -- a real session over a scripted boundary
# ---------------------------------------------------------------------------


@dataclass
class SessionOutcome:
    """What one session did, as facts a metric can be computed from."""

    index: int
    claimed_feature: str
    status: str
    changed_files: Tuple[str, ...] = ()
    commands_run: int = 0
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    brief: Optional[SessionBrief] = None
    finished_feature: str = ""
    reason: str = ""
    #: W2.1. Model turns actually SPENT. ``None`` when the body could not
    #: report one. A session that reports its budget as spent when nothing
    #: happened is the exact shape of measurement this module exists to refuse, so
    #: the counter is incremented where the turn happens and nowhere else.
    turns_used: Optional[int] = None
    #: W2.1. ``read_before_edit`` refusals this session actually received.
    #: ``None`` means the gate was never armed -- which is a DIFFERENT fact from
    #: "armed and refused nothing", and the two must not collapse into a zero.
    gate_refusals: Optional[int] = None
    #: W2.1. ``subagent_isolation`` receipt, when that arm's mechanism ran.
    isolation: Optional[Dict[str, Any]] = None
    #: W2.1. Characters in the window the PARENT actually held. This is what
    #: ``parent_context_tokens`` measures; measuring the BRIEF instead made the
    #: metric blind to the isolation arm, whose whole effect is on the parent's
    #: window and not on the brief.
    parent_chars: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "index": int(self.index),
            "claimed_feature": str(self.claimed_feature),
            "status": str(self.status),
            "changed_files": list(self.changed_files),
            "commands_run": int(self.commands_run),
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "finished_feature": str(self.finished_feature),
            "reason": str(self.reason),
            "turns_used": self.turns_used,
            "gate_refusals": self.gate_refusals,
            "parent_chars": self.parent_chars,
            "isolation": dict(self.isolation) if self.isolation is not None else None,
            "brief": self.brief.to_dict() if self.brief is not None else None,
        }


# ---------------------------------------------------------------------------
# The session body: a REAL, turn-bounded loop over the product's tool path
# ---------------------------------------------------------------------------


def _session_registry(root: Any) -> Any:
    """Return a real ``ToolRegistry`` with real ``read``/``write`` handlers.

    Every mutation a session makes therefore goes through
    ``ToolRegistry.execute``'s guard chain -- which is where the read-before-edit
    gate lives -- rather than through a ``path.write_text`` in this module. A
    session body that wrote files itself makes the ``read_before_edit`` arm
    structurally incapable of separating, which is precisely the defect W2.1 was
    called to remove.
    """
    from harness.agent_kernel.tools import ToolRegistry, ToolResult, builtin_tool_specs

    base = Path(str(root))

    def _read(call: Any, _ctx: Mapping[str, Any]) -> Any:
        """Read the file off disk and return its REAL bytes."""
        target = base / str(call.arguments.get("path") or "")
        try:
            return ToolResult(True, target.read_text(encoding="utf-8"))
        except OSError as exc:
            return ToolResult(False, f"read failed: {exc}", error_kind="no_match")

    def _write(call: Any, _ctx: Mapping[str, Any]) -> Any:
        """Write the file, as the product's write tool would."""
        relative = str(call.arguments.get("path") or "")
        content = str(call.arguments.get("content") or "")
        target = base / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            return ToolResult(False, f"write failed: {exc}", error_kind="handler_error")
        return ToolResult(True, f"wrote {relative} ({len(content)} chars)")

    registry = ToolRegistry()
    for spec in builtin_tool_specs():
        registry.register(spec)
    registry.restrict(("read", "write"))
    registry.set_handler("read", _read)
    registry.set_handler("write", _write)
    # **The ledger is constructed here, and this line is the whole point.**
    # ``ToolRegistry.read_ledger`` starts as ``None`` and is only created by
    # ``begin_turn``. Without it ``_check_read_before_edit`` returns ``None``
    # immediately -- "no ledger, no gate" -- so the registry's own receipt
    # reported ``armed_and_did_nothing: False`` while the gate had never had the
    # chance to refuse anything. A silent no-op gate reported as armed is exactly
    # the shape ``harness/agent_kernel/read_before_edit.py`` says a gate must not
    # be, and the first draft of this session hit it.
    registry.begin_turn(0)
    return registry


def _implementation(feature: str, *, previous: Optional[str] = None, package: str = "shop") -> str:
    """Return the minimal correct implementation body for one feature.

    ``previous`` is the predecessor in a DEPENDENCY CHAIN, and the import it
    produces is load-bearing for more than tidiness: it is what makes a later
    session's rewrite of ``previous`` able to break this feature's green.

    **This was a live measurement bug.** :func:`build_task_repo` writes the
    chain's import, and the first version of this function wrote a bare body --
    so the first write of every session ERASED the import and the
    ``dependency_chain`` task quietly became the ``independent_features`` task
    one turn in. The shape pin asserted the import on the freshly-built fixture
    and therefore passed for the whole wave. A pin that only measures the
    fixture measures a repository no session ever works in.
    """
    imports = (
        f"from {package}.{previous} import {previous}_value\n\n" if previous else ""
    )
    return (
        f"{imports}"
        f"def {feature}_value(items):\n"
        '    """Sum the argument list."""\n'
        "    return sum(items)\n"
    )


def _do_write(
    registry: Any,
    feature: str,
    *,
    turn: int,
    config: Mapping[str, Any],
    package: str = "shop",
    previous: Optional[str] = None,
) -> Any:
    """Dispatch ONE ``write`` for ``feature`` through the real guard chain.

    ``previous`` is the feature this one imports in a dependency-chain task; see
    :func:`_implementation` for why the import has to survive the write.
    """
    from shared.agent_contracts import ToolCall

    call = ToolCall(
        tool="write",
        arguments={
            "path": f"{package}/{feature}.py",
            "content": _implementation(feature, previous=previous, package=package),
        },
    )
    return registry.execute(call, {"config": dict(config), "turn": int(turn)})


def _do_read(
    registry: Any, feature: str, *, turn: int, config: Mapping[str, Any], package: str = "shop"
) -> Any:
    """Dispatch ONE ``read`` for ``feature`` through the real guard chain."""
    from shared.agent_contracts import ToolCall

    call = ToolCall(tool="read", arguments={"path": f"{package}/{feature}.py"})
    return registry.execute(call, {"config": dict(config), "turn": int(turn)})


def _bounded_session(
    task: Mapping[str, Any],
    root: Any,
    *,
    session_index: int,
    claimed_feature: str,
    config: Mapping[str, Any],
    session_turns: int,
    brief: Optional[SessionBrief],
    gate: Callable[[Mapping[str, str], str], bool],
    isolate: bool = False,
    initializer: bool = True,
    on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> SessionOutcome:
    """Run ONE session as a REAL, TURN-BOUNDED loop and return its facts.

    **The scripted part is the policy, not the execution.** The session decides
    *what to attempt*; every attempt is a real ``ToolRegistry.execute`` call
    through the product's guard chain, and the turn ceiling is ENFORCED here
    rather than recorded and ignored.

    The policy, and why it is this policy:

    1. **Attempt the write first.** A session that reads before writing is a
       session the read-before-edit gate can never refuse, so the arm would be
       void again. Writing first is also the realistic shape -- it is the class
       the gate exists to catch.
    2. **On a refusal, read, then write again.** That is what a real agent does
       with a tool error, and it makes the gate's cost MEASURABLE as a turn cost
       rather than as a failure. With the shipped three-turn budget: one turn on
       the refused attempt, one reading, one writing.
    3. **Verification is NOT a turn.** A verifier is not a model turn; charging
       it to the budget would make the budget a fiction. It runs after the loop.

    ``isolate=True`` moves steps 1-2 into a bounded subagent with its OWN
    message list, and the parent receives only a compressed summary. The
    subagent's own read ledger still applies, so the gate still fires -- and
    ``parent_context_tokens`` then measures the PARENT's window, which is the
    isolation claim rather than an assumption about it.
    """
    base = Path(str(root))
    package = str(task.get("package") or "shop")
    budget = max(1, int(session_turns))
    registry = _session_registry(base)
    state = {"turns": 0}
    armed = bool(config.get("read_before_edit")) or bool(config.get("read_before_edit_same_turn"))
    child_messages: List[Dict[str, str]] = []
    parent_messages: List[Dict[str, str]] = []
    # The predecessor in a dependency chain, resolved from the TASK's own
    # feature order. Resolving it here rather than inside ``_do_write`` is what
    # keeps the chain real through the session: see ``_implementation``.
    ordered = [str(item) for item in (task.get("features") or [])]
    _index = ordered.index(claimed_feature) if claimed_feature in ordered else -1
    previous = (
        ordered[_index - 1]
        if _index > 0 and str(task.get("shape") or "") == "dependency_chain"
        else None
    )

    def _turn() -> int:
        state["turns"] += 1
        return int(state["turns"])

    def _attempt() -> bool:
        """Write first; on a refusal, read, then write again."""
        first = _turn()
        result = _do_write(
            registry,
            claimed_feature,
            turn=first,
            config=config,
            package=package,
            previous=previous,
        )
        if bool(getattr(result, "ok", False)):
            child_messages.append({"role": "tool", "content": f"wrote {package}/{claimed_feature}.py"})
            return True
        child_messages.append(
            {"role": "tool", "content": str(getattr(result, "output", "") or "write refused")}
        )
        if state["turns"] >= budget:
            return False
        read_result = _do_read(registry, claimed_feature, turn=_turn(), config=config, package=package)
        child_messages.append(
            {"role": "tool", "content": str(getattr(read_result, "output", "") or "")[:400]}
        )
        if state["turns"] >= budget:
            return False
        retry = _do_write(
            registry,
            claimed_feature,
            turn=_turn(),
            config=config,
            package=package,
            previous=previous,
        )
        if bool(getattr(retry, "ok", False)):
            child_messages.append({"role": "tool", "content": f"wrote {package}/{claimed_feature}.py"})
            return True
        child_messages.append({"role": "tool", "content": str(getattr(retry, "output", "") or "")})
        return False

    parent_messages.append(
        {"role": "user", "content": brief.render() if brief is not None else ""}
    )
    isolation_receipt: Optional[Dict[str, Any]] = None
    wrote = False
    if isolate:
        from harness.agent_kernel import isolation as ISO

        wrote = _attempt()
        # ``compress_summary`` returns ``(text, receipt)`` -- the receipt says what
        # was DROPPED, which is the honest half and the reason to call the real
        # function rather than slicing a string here.
        summary, compression = ISO.compress_summary(
            "\n".join(str(item.get("content") or "") for item in child_messages),
            max_chars=ISO.SUMMARY_MAX_CHARS,
        )
        parent_messages.append({"role": "user", "content": summary})
        receipt = ISO.isolation_receipt(
            agent=f"feature-{claimed_feature}",
            messages=child_messages,
            returned=summary,
            parent_messages=len(parent_messages),
            parent_tokens=_tokens(
                sum(len(str(item.get("content") or "")) for item in parent_messages)
            ),
            truncated=bool(child_messages) and len(summary) >= ISO.SUMMARY_MAX_CHARS,
            notes="P4/W2 bounded session; the child's transcript never crosses this boundary",
        )
        payload = receipt.to_dict() if hasattr(receipt, "to_dict") else dict(receipt)
        payload["compression"] = compression
        isolation_receipt = payload
    else:
        wrote = _attempt()
        # **Without the isolation arm the child's messages ARE the parent's own
        # window.** Leaving them out made ``tokens_per_session`` blind to the
        # read-before-edit gate: the gate costs a refused write and a read, so
        # the session's context grows, and a metric that counted only the brief
        # reported three turns of work as costing the same as one. The first
        # draft had this and the gate arm was invisible in the token column.
        parent_messages.extend(child_messages)

    verify_ok = bool(gate(task, f"tests/test_{claimed_feature}.py")) if wrote else False

    changed = (
        tuple(
            sorted(
                [f"{package}/{claimed_feature}.py"]
                + (
                    [LH.PROGRESS_LEDGER_NAME]
                    if (base / LH.PROGRESS_LEDGER_NAME).is_file()
                    else []
                )
            )
        )
        if wrote
        else ()
    )
    # **Only when the initializer actually ran.** ``_note_session`` CREATES
    # ``PROGRESS.md`` if it is absent, so calling it unconditionally handed the
    # ``baseline`` arm a progress ledger it was never supposed to have -- which
    # then counted as a changed file in ``rework_rate`` and inflated the
    # baseline's parent context. An instrument that changes what it measures is
    # the failure this module exists to refuse, and it was in the measurement's
    # own control path.
    if wrote and initializer:
        _note_session(base, claimed_feature, verify_ok)
    report = registry.read_before_edit_report(config)
    # **Count the refusals from the registry's OWN log, not from the report.**
    # ``ToolRegistry.read_before_edit_report`` calls
    # ``harness.agent_kernel.read_before_edit.read_before_edit_report(self.read_ledger,
    # config)`` WITHOUT forwarding ``refusals=self.read_before_edit_refusals``, and the
    # module-level function's ``refusals`` parameter defaults to ``()``. So the
    # registry's receipt reports ``paths_refused: 0`` and ``calls_gated: 0`` after a
    # gate that genuinely refused -- a receipt that says the guard did nothing when it
    # did something, which is the same dishonesty this module exists to refuse.
    #
    # This is a CROSS-TERMINAL FINDING, not a local workaround: the fix belongs in
    # ``harness/agent_kernel/tools.py:433``, which T5 may not edit. It is filed in the
    # handoff with the exact one-line patch.
    refusals_log = list(getattr(registry, "read_before_edit_refusals", []) or ())
    refusals = sum(len(dict(item).get("refusals") or ()) for item in refusals_log)
    acknowledged = sum(
        1 for item in refusals_log if str(dict(item).get("outcome") or "") == "acknowledged"
    )
    refusals = int(refusals)
    return SessionOutcome(
        index=session_index,
        claimed_feature=str(claimed_feature),
        status="completed" if verify_ok else ("verify_failed" if wrote else "budget_exhausted"),
        changed_files=changed,
        commands_run=int(state["turns"]),
        tokens=_tokens(sum(len(str(item.get("content") or "")) for item in parent_messages)),
        cost_usd=None,
        brief=brief,
        finished_feature=str(claimed_feature) if verify_ok else "",
        turns_used=int(state["turns"]),
        gate_refusals=refusals if armed else None,
        parent_chars=sum(len(str(item.get("content") or "")) for item in parent_messages),
        isolation=isolation_receipt,
        reason=(
            f"{state['turns']} of {budget} turn(s) spent; read_before_edit refused "
            f"{refusals} path(s) ({acknowledged} acknowledged); the registry's own "
            f"read_before_edit_receipt reported paths_refused="
            f"{report.get('paths_refused')}, which UNDER-reports because the registry "
            "does not forward its refusal log (filed cross-terminal)"
            + ("; isolation receipt published" if isolation_receipt is not None else "")
            + "; scripted POLICY over a real tool path -- NOT model quality"
        ),
    )


def control_arm_turn_arithmetic() -> Dict[str, Any]:
    """Return the arithmetic that makes the control arm MEAN something.

    Published rather than assumed, because a control arm that "obviously" cannot
    finish is exactly the kind of thing that is checked once by hand and never
    again. Every number here is derived from the constants the run actually uses,
    and :func:`_one_effort` re-observes the CONSEQUENCE with a real pytest run on
    every execution.
    """
    features = len(FEATURE_NAMES)
    budget = int(DEFAULT_SESSION_TURNS)
    per_session_max = min(budget, features)
    gate_on = min(budget, 3)
    return {
        "features": features,
        "sessions_required": features,
        "session_turn_budget": budget,
        "max_features_one_session_can_finish": per_session_max,
        "one_session_cannot_finish": per_session_max < features,
        "gate_off_turns_per_feature": 1,
        "gate_on_turns_per_feature": gate_on,
        "gate_fits_the_budget": gate_on <= budget,
        "arithmetic": (
            f"one session at {budget} turns attempts at most {per_session_max} of "
            f"{features} features, so the single-session control must leave "
            f"{features - per_session_max} open; with read_before_edit armed the same "
            f"feature costs {gate_on} turns, which the {budget}-turn budget accommodates "
            "exactly -- so the gate is measurable as a TURN cost and not engineered "
            "into a completion failure"
        ),
    }



def _note_session(root: Path, feature: str, verify_ok: bool) -> None:
    """APPEND one session's line to the progress ledger, through T1's writer."""
    from harness.long_horizon import write_progress_ledger

    path = Path(root) / LH.PROGRESS_LEDGER_NAME
    existing = LH.load_progress_ledger(path).rstrip("\n")
    block = f"### session {feature}\n\n- worked on `{feature}`; verification {'green' if verify_ok else 'RED'}.\n"
    combined = (existing + "\n\n" + block) if existing else block
    write_progress_ledger(path, combined)


def _host_pytest_gate(task: Mapping[str, Any], target: str) -> bool:
    """Run the feature's own pytest node on the HOST and report whether it passed.

    The host lane, not the sandbox lane: this gate decides whether a FEATURE is
    done, and the nightly lane is where the Docker-backed repetitions run. The
    report carries which lane produced it, because a host pytest and a sandboxed
    pytest are not the same measurement.
    """
    import subprocess

    repo = str(task.get("repo") or "")
    if not repo:
        return False
    try:
        completed = subprocess.run(
            [os.sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-o", "addopts=", target],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# ---------------------------------------------------------------------------
# The metrics
# ---------------------------------------------------------------------------


def _tokens(chars: int) -> int:
    """Return a HEURISTIC token count, and the receipt says it is one."""
    return round(max(0, int(chars)) / CHARS_PER_TOKEN)


@dataclass
class Metric:
    """One metric, which may be unmeasured.

    ``value is None`` with a non-empty ``reason`` is the honest null. Rendering it
    as ``0`` would put a number in a table where nobody took one, which is the
    failure ``phases/DOCTRINE.md`` section 1 calls out by name.
    """

    name: str
    value: Optional[float]
    unit: str = ""
    reason: str = ""
    per_session: Tuple[Optional[float], ...] = ()

    @property
    def measured(self) -> bool:
        """Return whether this metric carries a number."""
        return self.value is not None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": str(self.name),
            "value": self.value,
            "unit": str(self.unit),
            "measured": self.measured,
            "reason": str(self.reason),
            "per_session": list(self.per_session),
        }

    def render(self) -> str:
        """Return one line; an unmeasured metric says WHY."""
        if self.measured:
            return f"{self.name}: {self.value}{self.unit}"
        return f"{self.name}: NOT MEASURED ({self.reason or 'no reason recorded'})"


def metric_completion(sessions: Sequence[SessionOutcome], *, features: Sequence[str]) -> Metric:
    """Did the effort finish every feature within the session budget?"""
    finished = {item.finished_feature for item in sessions if item.finished_feature}
    missing = [name for name in features if name not in finished]
    if not sessions:
        return Metric("completion", None, reason="no session ran")
    if not features:
        return Metric("completion", None, reason="the task declared no features")
    return Metric(
        "completion",
        1.0 if not missing else 0.0,
        unit="",
        reason="" if not missing else f"unfinished: {', '.join(missing)}",
        per_session=tuple(
            (1.0 if item.finished_feature else 0.0) for item in sessions
        ),
    )


def metric_rework_rate(sessions: Sequence[SessionOutcome]) -> Metric:
    """Fraction of session N's changed files that an EARLIER session had already touched.

    Rework is the question Phase 4 asks: *did session 3 redo session 1's work?* It
    is computed over each session's real changed set, read back off the
    filesystem, not over what a session claimed.
    """
    if len(sessions) < 2:
        return Metric("rework_rate", None, reason="rework needs at least two sessions")
    # Seed with the FIRST session's files. Iterating from session 2 with an empty
    # accumulator measures "did session N redo session N-1", not "did session N
    # redo earlier work" -- and the second is the question Phase 4 asks. (Caught by
    # the metric's own pin: with an empty seed every ratio was 0.0.)
    seen: set = set(sessions[0].changed_files)
    ratios: List[float] = []
    for item in sessions[1:]:
        touched = set(item.changed_files)
        if not touched:
            continue
        ratios.append(len(touched & seen) / len(touched))
        seen |= touched
    if not ratios:
        return Metric("rework_rate", None, reason="no session after the first changed a file")
    return Metric(
        "rework_rate",
        round(statistics.fmean(ratios), 4),
        unit="",
        reason="",
        per_session=tuple(ratios),
    )


def metric_constraint_retention(sessions: Sequence[SessionOutcome]) -> Metric:
    """Fraction of the constraints STATED IN SESSION 1 that still govern the last session.

    Measured against the last session's real changed set: a constraint is
    violated when that set touches a path the constraint names.

    The constraint set is read from the LAST session's BRIEF rather than from the
    module constant, and that is the whole point of the metric. The brief is what
    a session is actually given; an arm that never stated the constraints cannot
    retain them, and reporting ``0.0`` for that would score an arm for something
    it was never asked to do. It reports ``None`` with the reason, which is both
    the honest answer and a finding about the baseline arm.
    """
    if len(sessions) < 2:
        return Metric("constraint_retention", None, reason="retention needs at least two sessions")
    final = sessions[-1]
    stated = tuple(final.brief.constraints) if final.brief is not None else ()
    if not stated:
        return Metric(
            "constraint_retention",
            None,
            reason=(
                "no constraint set was stated in any session's brief, so there is nothing to "
                "retain; the baseline arm has no initializer and therefore no durable rules"
            ),
        )
    if not final.changed_files:
        return Metric(
            "constraint_retention",
            None,
            reason=f"session {final.index} changed no file, so there is nothing to judge",
        )
    from fnmatch import fnmatch

    honoured = 0
    violated: List[str] = []
    for constraint_id, pattern, _why in stated:
        hit = [path for path in final.changed_files if fnmatch(path, pattern.replace("\\", "/"))]
        if hit:
            violated.append(f"{constraint_id}:{','.join(hit)}")
        else:
            honoured += 1
    share = honoured / len(stated)
    return Metric(
        "constraint_retention",
        round(share, 4),
        unit="",
        reason="; ".join(violated),
        per_session=(share,),
    )


def metric_parent_context_tokens(sessions: Sequence[SessionOutcome]) -> Metric:
    """Tokens the PARENT context carried, per task.

    "Parent" is the durable brief a session starts from **plus everything the
    parent window went on to hold** -- the tool results the session itself read
    back. Measuring a per-session conversation instead of that window would be
    measuring compaction, and measuring the brief ALONE would be measuring the
    initializer rather than the session: the first draft of W2.1 did the latter
    and the ``subagent_isolation`` arm was structurally invisible in this column,
    because isolation changes what the parent holds and not what the brief says.

    Falls back to the brief's own rendered length when a session could not report
    a window, and says so in the reason rather than quietly substituting a
    different quantity.
    """
    values: List[int] = []
    fell_back = False
    for item in sessions:
        if item.parent_chars is not None:
            values.append(int(item.parent_chars))
            continue
        if item.brief is not None:
            fell_back = True
            values.append(len(item.brief.render()))
    if not values:
        return Metric("parent_context_tokens", None, reason="no session built a window")
    return Metric(
        "parent_context_tokens",
        round(statistics.fmean([_tokens(value) for value in values])),
        unit=" (heuristic)",
        reason=(
            ""
            if not fell_back
            else "PARTIAL: at least one session reported no window, so the brief's rendered "
            "length was substituted for it -- the two are different quantities"
        ),
        per_session=tuple(_tokens(value) for value in values),
    )


def metric_tokens_per_session(sessions: Sequence[SessionOutcome]) -> Metric:
    """Mean tokens per session -- the efficiency claim, measured."""
    values = [item.tokens for item in sessions if item.tokens is not None]
    if not values:
        return Metric("tokens_per_session", None, reason="no session reported usage")
    return Metric(
        "tokens_per_session",
        round(statistics.fmean(values)),
        unit=" (heuristic)",
        reason="",
        per_session=tuple(values),
    )


def metric_cost_usd(sessions: Sequence[SessionOutcome]) -> Metric:
    """Total cost across the sessions.

    ``None`` for the scripted runner, and that is the point: a scripted boundary
    has no price, and reporting ``$0.0000`` would put a zero in a cost column
    that reads like a measurement. The nightly lane's live-provider repetitions
    are where this becomes a number.
    """
    values = [item.cost_usd for item in sessions if item.cost_usd is not None]
    if not values:
        return Metric(
            "cost_usd",
            None,
            reason="the scripted model boundary reports no price; a $0.00 here would be a fabrication",
        )
    return Metric("cost_usd", round(sum(values), 6), unit=" USD", reason="", per_session=tuple(values))


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class ArmResult:
    """One arm over ``repetitions`` repetitions, with the six metrics."""

    arm: str
    config: Dict[str, Any]
    verdict: str
    metrics: Dict[str, Metric] = field(default_factory=dict)
    repetitions: Tuple[Dict[str, Any], ...] = ()
    notes: Tuple[str, ...] = ()
    #: W2.1. The per-session FACTS of the most recent repetition: turns used,
    #: gate refusals, the isolation receipt, the window size. Published because a
    #: reader cannot tell "the gate fired" from "the gate was never armed" without
    #: it, and the first version kept them in a note string and lost the structure.
    sessions: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "arm": str(self.arm),
            "config": dict(self.config),
            "verdict": str(self.verdict),
            "metrics": {name: metric.to_dict() for name, metric in self.metrics.items()},
            "repetitions": [dict(item) for item in self.repetitions],
            "sessions": [dict(item) for item in self.sessions],
            "notes": list(self.notes),
            "model_quality_measured": MODEL_QUALITY_MEASURED,
        }

    def render(self) -> str:
        """Return plain lines: the six metrics, each with its own null reason."""
        lines = [f"arm {self.arm}: {self.verdict}"]
        for name in METRIC_NAMES:
            metric = self.metrics.get(name)
            if metric is None:
                lines.append(f"  {name}: ABSENT (not computed)")
                continue
            lines.append("  " + metric.render())
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


def arm_config(arm: str) -> Dict[str, Any]:
    """Return one arm's config overrides, refusing an unknown arm BY NAME.

    An unknown arm returning ``{}`` would run the BASELINE and publish it as the
    requested arm, which is the specific dishonesty an arm table is exposed to
    (``evals/AGENTS.md`` records the same reasoning for
    ``harness.prompt_budget_measure.eval_arm_recipe``).
    """
    if str(arm) not in MULTI_SESSION_ARMS:
        raise KeyError(
            f"unknown multi-session arm {arm!r}; known arms are {sorted(MULTI_SESSION_ARMS)}"
        )
    return dict(MULTI_SESSION_ARMS[str(arm)])


def opt_in_keys_not_in_defaults() -> Tuple[str, ...]:
    """Return every arm key that appears in ``harness.config.DEFAULTS``.

    The expected answer is ``()``, and it is a RETURNED VALUE rather than a
    raise so the pin can assert on the specific offenders. A key published in
    ``DEFAULTS`` would switch EVERY task and EVERY eval arm on the day it landed,
    which is why these arms live in their own table.
    """
    from harness.config import DEFAULTS

    return tuple(sorted(key for key in OPT_IN_KEYS if key in DEFAULTS))


#: The arm each one-MECHANISM arm is compared against. ``baseline`` is the
#: control for the whole table, and ``initializer`` is the control for the two
#: that add a mechanism on top of it.
CONTROL_ARM: str = "baseline"
INITIALIZER_ARM: str = "initializer"

#: **Which mechanism each key belongs to.** The one-variable rule is about
#: mechanisms, not about raw key counts: the initializer is ONE mechanism
#: expressed in three keys (``initialize`` + ``ledgers`` + ``one_feature_per_
#: session``), and T1's design declares them as a bundle -- splitting them would
#: mean measuring a configuration nobody ships.
#:
#: The mapping is DATA rather than a prefix rule so that a new key has to be
#: classified deliberately. An unclassified key maps to itself, which makes
#: :func:`unclassified_arm_keys` report it -- an arm whose new key nobody
#: classified is an arm nobody can reason about, and that is worth a finding
#: rather than a silent pass.
MECHANISM_GROUPS: Dict[str, str] = {
    "long_horizon_initialize": "initializer",
    "long_horizon_ledgers": "initializer",
    "long_horizon_one_feature_per_session": "initializer",
    "read_before_edit": "read_before_edit",
    "read_before_edit_same_turn": "read_before_edit",
    "subagent_isolation": "subagent_isolation",
}


def unclassified_arm_keys() -> Tuple[str, ...]:
    """Return arm keys that :data:`MECHANISM_GROUPS` does not classify.

    Expected answer is ``()``. A key nobody filed under a mechanism means the
    one-variable rule cannot be evaluated for the arm that sets it, so this is a
    finding rather than a detail.
    """
    return tuple(key for key in OPT_IN_KEYS if key not in MECHANISM_GROUPS)


def arm_mechanisms(arm: str, *, control: str) -> Tuple[str, ...]:
    """Return the MECHANISMS on which ``arm`` differs from ``control``.

    **This is the one-variable rule, as a function rather than a claim.** An arm
    that also changed the tool surface or the prompt would be void, and the way
    that happens is quietly -- so the delta is computed from the arm table itself
    and published on every report.
    """
    left = arm_config(arm)
    right = arm_config(control)
    changed = [
        key for key in set(left) | set(right) if left.get(key) != right.get(key)
    ]
    return tuple(sorted({MECHANISM_GROUPS.get(key, key) for key in changed}))


def arm_variable_keys(arm: str, *, control: str) -> Tuple[str, ...]:
    """Return the raw config keys on which ``arm`` differs from ``control``."""
    left = arm_config(arm)
    right = arm_config(control)
    return tuple(sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key)))


def control_arm_for(arm: str) -> str:
    """Return the control an arm is compared against, DERIVED not declared.

    An arm that is the initializer **plus one more mechanism** is compared
    against the initializer; anything else is compared against the empty
    ``baseline``. Deriving it matters: the P4/W1 version compared both mechanism
    arms against ``baseline``, so each of them differed from its control in FOUR
    keys and no arm in the table could honestly claim to isolate one variable.
    The rule is ``the arm's mechanisms minus the control's, exactly one``.
    """
    if str(arm) == CONTROL_ARM:
        return INITIALIZER_ARM
    if set(arm_mechanisms(str(arm), control=INITIALIZER_ARM)) and len(
        arm_mechanisms(str(arm), control=INITIALIZER_ARM)
    ) == 1:
        return INITIALIZER_ARM
    return CONTROL_ARM


def arm_realness(control: Optional[str] = None) -> Dict[str, Any]:
    """Return a MECHANICAL verdict on whether every arm is a real arm.

    Three separate failure modes, kept separate because they are separate
    defects and a single boolean would collapse them:

    1. ``one_variable`` -- the arm differs from its DERIVED control in exactly
       one mechanism (:func:`arm_mechanisms`). An arm differing in two
       mechanisms is void: a comparison against it cannot attribute the
       difference to either.
    2. ``mechanism_read`` -- the mechanism is read by something the session body
       actually CALLS. This is the check P4/W1 could not make: its body wrote
       files directly, so ``read_before_edit`` and ``subagent_isolation`` were
       read by nothing on the path a session took and both arms were
       structurally incapable of separating. It is re-derived here by calling
       the two real readers with the arm's config and requiring an ANSWER.
    3. ``keys_in_defaults`` -- no key the arm sets is published in
       ``harness.config.DEFAULTS``, or publishing it would have silently
       switched every run in the project.

    ``all_arms_real`` is the conjunction. It is published on every report.
    """
    rows: List[Dict[str, Any]] = []
    for name in sorted(MULTI_SESSION_ARMS):
        baseline = str(control) if control else control_arm_for(name)
        keys = arm_variable_keys(name, control=baseline)
        mechanisms = arm_mechanisms(name, control=baseline)
        mechanism = mechanisms[0] if len(mechanisms) == 1 else ""
        rows.append(
            {
                "arm": name,
                "control": baseline,
                "one_variable": len(mechanisms) == 1,
                "mechanisms": list(mechanisms),
                "variable_keys": list(keys),
                "mechanism": mechanism,
                "mechanism_read": _mechanism_is_read(mechanism) if mechanism else False,
                "keys_in_defaults": [key for key in keys if key in _defaults_keys()],
            }
        )
    return {
        "arms": rows,
        "all_arms_real": all(
            row["one_variable"] and row["mechanism_read"] and not row["keys_in_defaults"]
            for row in rows
        ),
        "unclassified_keys": list(unclassified_arm_keys()),
        "mechanism_groups": dict(MECHANISM_GROUPS),
        "method": (
            "one_variable counts MECHANISMS (MECHANISM_GROUPS), not raw keys, because the "
            "initializer is one mechanism expressed in three keys; the raw keys are "
            "published alongside. mechanism_read CALLS the module that owns the mechanism "
            "with this arm's config and requires an answer. No arm is exempt from the "
            "mechanism_read check -- that exemption is what made two P4/W1 arms void."
        ),
    }


def _defaults_keys() -> Tuple[str, ...]:
    """Return the keys published in ``harness.config.DEFAULTS``, or ``()``."""
    try:
        from harness.config import DEFAULTS

        return tuple(sorted(DEFAULTS))
    except Exception:
        return ()


def _mechanism_is_read(mechanism: str) -> bool:
    """Return whether the real reader of ``mechanism`` answers.

    Driven by the MECHANISM name, not the arm name, so a caller asking about an
    arbitrary key gets an answer about that mechanism. An unrecognised mechanism
    is ``False`` -- an arm whose mechanism nothing reads is void, and the safe
    direction for "is this mechanism wired?" is ``False``.
    """
    try:
        if mechanism == "read_before_edit":
            from harness.agent_kernel import read_before_edit as RBE

            return RBE.read_before_edit_enabled({"read_before_edit": True}) is True
        if mechanism == "subagent_isolation":
            from harness.agent_kernel import isolation as ISO

            decision = ISO.split_decision(
                [{"id": str(name), "self_contained": True} for name in FEATURE_NAMES]
            )
            return bool(getattr(decision, "way", "")) and bool(ISO.SPLIT_WAYS)
        if mechanism == "initializer":
            # The initializer is answered by the initializer itself; the check is
            # that it PROMISES the artefacts the arm depends on AND that the
            # entry point still exists, so an initializer that stopped writing
            # init.sh fails here rather than silently measuring the baseline.
            return (
                callable(getattr(LH, "initialize_project", None))
                and bool(getattr(LH, "INIT_SCRIPT_NAME", ""))
                and bool(getattr(LH, "FEATURE_LEDGER_NAME", ""))
                and bool(getattr(LH, "PROGRESS_LEDGER_NAME", ""))
            )
    except Exception:
        return False
    return False


def _arm_mechanism_wired(config: Mapping[str, Any], task: Mapping[str, Any], root: Path) -> Optional[str]:
    """Return why an arm's mechanism is not wired, or ``None`` when it is.

    A mechanism that is configured but INERT is the failure this whole
    evaluation is exposed to: the arm would run, produce numbers, and every
    number would be an artefact of the baseline. So the check is against the
    ARTEFACTS the mechanism is supposed to leave, not against the config key.
    """
    wants_initializer = bool(config.get("long_horizon_initialize"))
    has_init = (root / LH.INIT_SCRIPT_NAME).is_file()
    if wants_initializer and not has_init:
        return (
            f"{sorted(config)[0]}: the arm asks for the initializer but no {LH.INIT_SCRIPT_NAME} "
            "was produced, so the arm would measure the baseline"
        )
    if wants_initializer and not (root / LH.FEATURE_LEDGER_NAME).is_file():
        return f"the arm asks for the ledgers but no {LH.FEATURE_LEDGER_NAME} was produced"
    return None


def run_arm(
    arm: str,
    *,
    task_slugs: Sequence[str] = (),
    sessions: int = DEFAULT_SESSIONS,
    session_turns: int = DEFAULT_SESSION_TURNS,
    repetitions: int = DEFAULT_REPETITIONS,
    gate: Optional[Callable[[Mapping[str, str], str], bool]] = None,
    out_root: Any = "",
) -> ArmResult:
    """Run one arm ``repetitions`` times and return its six metrics.

    **One run is noise**, so the default is
    :data:`DEFAULT_REPETITIONS` and the report publishes the median plus the
    observed spread. A metric that is ``None`` in EVERY repetition stays ``None``
    -- it is not averaged into a number.

    The **control** runs first: a single session with the SAME per-session turn
    budget. If it finishes every feature, the task does not require multiple
    sessions and the arm's verdict is ``task_fits_one_session`` -- which is a
    refusal to produce a finding, not a result.
    """
    config = arm_config(arm)
    verify = gate or _host_pytest_gate
    notes: List[str] = []
    per_repetition: List[Dict[str, Any]] = []
    collected: Dict[str, List[Optional[float]]] = {name: [] for name in METRIC_NAMES}
    reasons: Dict[str, str] = {}
    control_fits = False
    latest_sessions: Tuple[Dict[str, Any], ...] = ()

    tasks: Tuple[str, ...] = tuple(task_slugs) if task_slugs else tuple(TASK_SLUGS)
    unknown_tasks = sorted(set(tasks) - set(TASK_FEATURE_NAMES))
    if unknown_tasks:
        raise KeyError(
            f"unknown multi-session task(s) {unknown_tasks}; known tasks are "
            f"{sorted(TASK_FEATURE_NAMES)}"
        )
    for repetition in range(max(1, int(repetitions))):
        # One REPETITION is one pass over EVERY task. A repetition is not a
        # repetition of one task -- it is a repetition of the whole measurement,
        # so a reader dividing repetitions by tasks is dividing by the wrong
        # thing. Per-task metrics are aggregated WITHIN the repetition and the
        # per-task values are kept beside the aggregate, because a mean across
        # two tasks is a different quantity from either task's number.
        per_task: Dict[str, Dict[str, Optional[float]]] = {}
        task_sessions: List[Dict[str, Any]] = []
        for task_slug in tasks:
            outcome, root = _one_effort(
                task=None,
                task_slug=str(task_slug),
                arm_config=config,
                sessions=sessions,
                session_turns=session_turns,
                gate=verify,
                out_root=out_root,
                repetition=repetition,
            )
            notes.extend(outcome.notes)
            control_fits = control_fits or outcome.verdict == "task_fits_one_session"
            per_task[str(task_slug)] = {
                name: (
                    outcome.metrics[name].value
                    if name in outcome.metrics and outcome.metrics[name].measured
                    else None
                )
                for name in METRIC_NAMES
            }
            for session in outcome.sessions:
                task_sessions.append({**dict(session), "task": str(task_slug)})
            _cleanup(root)

        latest_sessions = tuple(task_sessions)
        for name in METRIC_NAMES:
            values = [
                per_task[slug][name] for slug in per_task if per_task[slug][name] is not None
            ]
            collected[name].append(
                round(statistics.fmean(values), 6) if values else None
            )
            missing = [
                slug for slug in per_task if per_task[slug][name] is None
            ]
            if missing:
                reasons.setdefault(
                    name,
                    f"not measured on task(s) {sorted(missing)}; see the per-task values",
                )
        per_repetition.append(
            {
                "repetition": repetition,
                "tasks": sorted(per_task),
                "per_task_metrics": per_task,
                "sessions": task_sessions,
            }
        )

    metrics: Dict[str, Metric] = {}
    for name in METRIC_NAMES:
        values = [value for value in collected[name] if value is not None]
        if not values:
            metrics[name] = Metric(
                name, None, unit=METRIC_UNITS.get(name, ""), reason=reasons.get(name, "not computed")
            )
            continue
        metrics[name] = Metric(
            name,
            round(statistics.median(values), 6),
            unit=METRIC_UNITS.get(name, ""),
            reason="",
            per_session=tuple(collected[name]),
        )

    verdict = "task_fits_one_session" if control_fits else "measured"
    if not control_fits and not any(metric.measured for metric in metrics.values()):
        verdict = "not_run"
    return ArmResult(
        arm=str(arm),
        config=config,
        verdict=verdict,
        metrics=metrics,
        repetitions=tuple(per_repetition),
        # W2.1: carry the LAST repetition's per-session facts through, so the
        # report publishes turns-used, gate refusals and the isolation receipt as
        # STRUCTURE rather than as a sentence inside ``notes``. Without this the
        # structured data existed and was dropped on the floor.
        sessions=tuple(latest_sessions),
        notes=tuple(sorted(set(notes))),
    )


#: The unit each metric is rendered in. Published so a reader does not have to
#: infer whether a number is a count or a rate.
METRIC_UNITS: Dict[str, str] = {
    "completion": "",
    "rework_rate": " (share)",
    "constraint_retention": " (share)",
    "parent_context_tokens": " (heuristic)",
    "tokens_per_session": " (heuristic)",
    "cost_usd": " USD",
}


def _one_effort(
    *,
    task: Optional[Mapping[str, Any]],
    task_slug: str = "",
    arm_config: Mapping[str, Any],
    sessions: int,
    session_turns: int,
    gate: Callable[[Mapping[str, str], str], bool],
    out_root: Any,
    repetition: int,
) -> Tuple[ArmResult, Path]:
    """Run ONE multi-session effort on its own fresh repository.

    The control runs inside here, before the multi-session arms, and its verdict
    rides on the returned :class:`ArmResult` so the caller does not have to
    remember to run it.
    """
    parent = Path(str(out_root)) if out_root else Path(tempfile.mkdtemp(prefix="neo-multisession-"))
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / f"repo-r{repetition}"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    built = build_task_repo(root, slug=str(task_slug or DEFAULT_TASK_SLUG))
    features = [feature.id for feature in task_features(built)]

    initializer = bool(arm_config.get("long_horizon_initialize"))
    store: Optional[FL.LedgerStore] = None
    if initializer:
        receipt = LH.initialize_project(
            str(root),
            LH.ProjectProfile(
                name=built["slug"],
                language="python",
                test_command="python -m pytest -q",
                lint_command="",
            ),
            task_features(built),
            request=str(built["request"]),
            project_id=str(built["slug"]),
            session="s0",
            git_runner=None,
        )
        if not receipt.complete:
            # Degraded on purpose: `git_runner=None` is the honest answer for a
            # driver that has no git bound, and the initializer REPORTS it rather
            # than claiming a commit it did not make.
            pass
        opened, refusal = FL.LedgerStore.open(root)
        store = opened
        if store is None and refusal is not None:
            return (
                ArmResult(
                    arm="(unrun)",
                    config=dict(arm_config),
                    verdict="arm_mechanism_not_wired",
                    metrics={name: Metric(name, None, reason=refusal.detail) for name in METRIC_NAMES},
                    notes=(refusal.render(),),
                ),
                root,
            )
        if store is not None:
            store.ensure_sealed(sealed_at="2026-10-06T00:00:00Z")

    unwired = _arm_mechanism_wired(arm_config, built, root)
    if unwired is not None:
        return (
            ArmResult(
                arm="(unrun)",
                config=dict(arm_config),
                verdict="arm_mechanism_not_wired",
                metrics={name: Metric(name, None, reason=unwired) for name in METRIC_NAMES},
                notes=(unwired,),
            ),
            root,
        )

    # -- THE CONTROL: ONE session, the SAME per-session turn budget --------
    #
    # W2.1 changed this. The P4/W1 control ran a single session over ONE feature
    # and then asked whether the OTHER features were still failing, which
    # demonstrated nothing: of course they were, the control had never tried
    # them. The control now asks the question the brief actually asks --
    #
    #     "given the SAME per-session turn budget, can ONE session finish?"
    #
    # -- and it is a real single session that keeps working through features
    # until its budget is gone. If it finishes every feature, the task does not
    # require multiple sessions and every multi-session number would be a
    # measurement of the control. That verdict is a REFUSAL to produce a finding.
    control = _run_control_on_a_throwaway_copy(
        built,
        root,
        slug=str(task_slug or DEFAULT_TASK_SLUG),
        initializer=initializer,
        arm_config=arm_config,
        features=features,
        session_turns=int(session_turns),
        gate=gate,
    )
    if control["finished_all"]:
        return (
            ArmResult(
                arm="(control)",
                config=dict(arm_config),
                verdict="task_fits_one_session",
                metrics={
                    name: Metric(
                        name,
                        None,
                        reason=(
                            "the task fits one session, so every multi-session number would "
                            "measure the control"
                        ),
                    )
                    for name in METRIC_NAMES
                },
                notes=(
                    "the task does not require multiple sessions; rebuild it",
                    f"control spent {control['turns_spent']} turn(s) and finished "
                    f"{control['finished']}/{len(features)} feature(s)",
                ),
            ),
            root,
        )
    control_note = (
        f"CONTROL: one session, {control['turns_spent']}/{int(session_turns)} turn(s), "
        f"finished {control['finished']}/{len(features)} feature(s), left open: "
        f"{', '.join(control['still_open']) or '(none)'}. The task therefore does NOT fit "
        "one session, which is what makes the multi-session numbers mean anything."
    )

    # -- the multi-session run ---------------------------------------------
    outcomes: List[SessionOutcome] = []
    for index in range(1, max(2, int(sessions)) + 1):
        feature = features[(index - 1) % len(features)]
        brief = build_session_brief(
            built, root, session_index=index, claimed_feature=feature, initializer=initializer
        )
        outcome = _bounded_session(
            built,
            root,
            session_index=index,
            claimed_feature=feature,
            config=arm_config,
            session_turns=int(session_turns),
            brief=brief,
            gate=gate,
            isolate=bool(arm_config.get("subagent_isolation")),
            initializer=initializer,
        )
        outcomes.append(outcome)
        if store is not None:
            _publish(store, outcome)

    metrics = {
        "completion": metric_completion(outcomes, features=features),
        "rework_rate": metric_rework_rate(outcomes),
        "constraint_retention": metric_constraint_retention(outcomes),
        "parent_context_tokens": metric_parent_context_tokens(outcomes),
        "tokens_per_session": metric_tokens_per_session(outcomes),
        "cost_usd": metric_cost_usd(outcomes),
    }
    turns = [item.turns_used for item in outcomes if item.turns_used is not None]
    refusals = [item.gate_refusals for item in outcomes if item.gate_refusals is not None]
    return (
        ArmResult(
            arm="(measured)",
            config=dict(arm_config),
            verdict="measured",
            metrics=metrics,
            sessions=tuple(item.to_dict() for item in outcomes),
            notes=(
                control_note,
                "turns_used per session: "
                + (", ".join(str(value) for value in turns) or "(not reported)"),
                "read_before_edit refusals per session: "
                + (
                    ", ".join(str(value) for value in refusals)
                    if refusals
                    else "(gate not armed in this arm -- NOT 'armed and refused nothing')"
                ),
            ),
        ),
        root,
    )


def _run_control_on_a_throwaway_copy(
    task: Mapping[str, Any],
    root: Any,
    *,
    slug: str = "",
    initializer: bool,
    arm_config: Mapping[str, Any],
    features: Sequence[str],
    session_turns: int,
    gate: Callable[[Mapping[str, str], str], bool],
) -> Dict[str, Any]:
    """Run the single-session control on its OWN copy of the repository.

    **The copy is load-bearing and the first version got it wrong.** The control
    writes real implementations; run on the same tree, it would leave three of
    the four features already passing, and the multi-session run would then
    complete a task that was already done. The control would have manufactured
    the very result it is supposed to test. So it gets a sibling repository,
    built by the SAME :func:`build_task_repo`, and the caller's tree is untouched.

    The failure mode this guards against is the same one the whole module is
    about: a measurement whose instrument changes what it measures.
    """
    parent = Path(str(root)).parent
    control_root = parent / f"control-{Path(str(root)).name}"
    if control_root.exists():
        shutil.rmtree(control_root, ignore_errors=True)
    built = build_task_repo(control_root, slug=slug)
    try:
        if initializer:
            # The control must face the SAME ledgers the arm faces, or it is
            # measuring a different project rather than a smaller budget.
            LH.initialize_project(
                str(control_root),
                LH.ProjectProfile(
                    name=str(built["slug"]),
                    language="python",
                    test_command="python -m pytest -q",
                    lint_command="",
                ),
                task_features(built),
                request=str(built["request"]),
                project_id=str(built["slug"]),
                session="control-s0",
                git_runner=None,
            )
        return _single_session_control(
            built,
            control_root,
            arm_config=arm_config,
            features=features,
            session_turns=session_turns,
            gate=gate,
            initializer=initializer,
            isolate=bool(arm_config.get("subagent_isolation")),
        )
    finally:
        shutil.rmtree(control_root, ignore_errors=True)


def _single_session_control(
    task: Mapping[str, Any],
    root: Any,
    *,
    arm_config: Mapping[str, Any],
    features: Sequence[str],
    session_turns: int,
    gate: Callable[[Mapping[str, str], str], bool],
    initializer: bool,
    isolate: bool = False,
) -> Dict[str, Any]:
    """Run ONE session with the SAME turn budget and report what it finished.

    A real bounded session, not a single-feature probe: it takes a brief, then
    works through features until the budget is spent, then stops. The answer is
    OBSERVED with the same real pytest gate every session uses, so
    ``still_open`` is a list of tests that genuinely still fail.

    The state it leaves behind is undone by the caller: the caller runs this on a
    throwaway repository, exactly as the control requires.
    """
    base = Path(str(root))
    budget = max(1, int(session_turns))
    spent = 0
    finished = 0
    still_open: List[str] = []
    for name in features:
        if spent >= budget:
            break
        brief = build_session_brief(
            task, base, session_index=1, claimed_feature=str(name), initializer=initializer
        )
        outcome = _bounded_session(
            task,
            base,
            session_index=1,
            claimed_feature=str(name),
            config=arm_config,
            session_turns=budget - spent,
            brief=brief,
            gate=gate,
            isolate=isolate,
            initializer=initializer,
        )
        spent += int(outcome.turns_used or 0)
        if outcome.finished_feature:
            finished += 1
    for name in features:
        if not gate(task, f"tests/test_{name}.py"):
            still_open.append(str(name))
    return {
        "turns_spent": spent,
        "turn_budget": budget,
        "finished": finished,
        "features": len(features),
        "still_open": tuple(still_open),
        "finished_all": not still_open,
    }


def _publish(store: FL.LedgerStore, outcome: SessionOutcome) -> None:
    """Record the session's verification through the STORE, evidence and all.

    The evidence is a real recorded observation of the gate's answer, and the
    status change goes through :meth:`LedgerStore.apply_status`, so the store's
    rules -- evidence required, command matched, transition allowed, reason
    present -- gate this evaluation exactly as they gate the product.
    """
    if not outcome.finished_feature:
        return
    digest, refusal = store.record_evidence(
        FL.EvidenceRecord(
            feature_id=outcome.finished_feature,
            outcome="pass" if outcome.status == "completed" else "fail",
            kind="run",
            ran=True,
            command=f"python -m pytest -q tests/test_{outcome.finished_feature}.py",
            exit_code=0 if outcome.status == "completed" else 1,
            observed_at="2026-10-06T00:00:05Z",
            session=f"s{outcome.index}",
        )
    )
    if refusal is not None:
        return
    store.apply_status(
        outcome.finished_feature,
        LH.PASSING_FEATURE_STATUS,
        evidence_digest=digest,
        actor=f"s{outcome.index}",
        reason=f"session {outcome.index} finished {outcome.finished_feature} and its own check passed",
    )


def _cleanup(root: Path) -> None:
    """Remove a per-repetition repository, best-effort.

    Best-effort on purpose: a cleanup failure must not turn a measurement into an
    error, and a leftover temporary directory is not a result.
    """
    shutil.rmtree(Path(root), ignore_errors=True)


# ---------------------------------------------------------------------------
# The report + the CLI
# ---------------------------------------------------------------------------


def isolation_summary(arms: Sequence["ArmResult"]) -> Dict[str, Any]:
    """Return what the ``subagent_isolation`` arm actually isolated, or WHY not.

    **This exists because a null here is easy to misread.** The isolation arm's
    six metrics came out IDENTICAL to the initializer arm's on the first W2.1 run.
    Two readings are available and they mean opposite things:

    * "isolation saves nothing" (a claim about the mechanism), or
    * "this workload gave isolation nothing to isolate" (a claim about the
      CORPUS).

    They are told apart by looking at the child's own transcript against the cap
    :func:`harness.agent_kernel.isolation.compress_summary` would have applied.
    On the measured run the child's whole window was **~22 characters against an
    8 000-character limit**: nothing was dropped because there was nothing to
    drop. So the null is a fact about a four-module synthetic shop, and saying
    otherwise would be reporting a corpus limitation as a mechanism result.
    """
    rows: List[Dict[str, Any]] = []
    for arm in arms:
        for session in arm.sessions:
            receipt = session.get("isolation") if isinstance(session, Mapping) else None
            if not receipt:
                continue
            rows.append(
                {
                    "arm": str(arm.arm),
                    "session": int(session.get("index") or 0),
                    "transcript_chars": receipt.get("transcript_chars"),
                    "returned_chars": receipt.get("returned_chars"),
                    "compression_ratio": receipt.get("compression_ratio"),
                    "truncated": bool(receipt.get("truncated")),
                    "dropped_chars": (receipt.get("compression") or {}).get("dropped_chars"),
                    "limit_chars": (receipt.get("compression") or {}).get("limit"),
                    "parent_chars": session.get("parent_chars"),
                }
            )
    if not rows:
        return {
            "receipts": [],
            "isolation_exercised": False,
            "reason": (
                "no session in any requested arm published an isolation receipt, so the "
                "subagent_isolation mechanism did not run and NOTHING is claimed about it"
            ),
        }
    dropped = sum(int(row["dropped_chars"] or 0) for row in rows)
    worst = max(int(row["transcript_chars"] or 0) for row in rows)
    limit = max(int(row["limit_chars"] or 0) for row in rows if row["limit_chars"]) or 1
    return {
        "receipts": rows,
        "isolation_exercised": True,
        "sessions_with_receipts": len(rows),
        "total_dropped_chars": dropped,
        "worst_transcript_chars": worst,
        "limit_chars": limit,
        "headroom_used": round(worst / limit, 6),
        "verdict": (
            "EXERCISED_BUT_NOT_STRESSED"
            if dropped == 0
            else "EXERCISED_AND_COMPRESSED"
        ),
        "reason": (
            ""
            if dropped
            else (
                f"the largest child transcript was {worst} characters against a "
                f"{limit}-character limit ({round(100.0 * worst / limit, 2)}% of the cap), "
                "so compression dropped NOTHING. The mechanism ran and was never stressed: "
                "this is a property of a four-module synthetic shop, NOT evidence that "
                "subagent isolation fails to save context. A workload whose child "
                "transcripts actually fill a context window is required to measure the "
                "claim, and that corpus does not exist here."
            )
        ),
    }


@dataclass
class MultiSessionReport:
    """The whole measurement, for every arm asked for."""

    arms: Tuple[ArmResult, ...]
    sessions: int
    repetitions: int
    session_turns: int

    @property
    def measured(self) -> bool:
        """Return whether any arm produced a number."""
        return any(
            metric.measured
            for arm in self.arms
            for metric in arm.metrics.values()
        )

    def discrimination(self) -> Dict[str, Any]:
        """Which metrics actually SEPARATE the arms, and which cannot.

        **The most important receipt in this module.** A metric whose value is
        identical in every arm measured nothing about the arms, and a reader who
        sees ``completion: 1.0`` under four arms would reasonably conclude the
        mechanism does not matter. ``varies`` is the direct answer;
        ``measurable`` separates "the metric carried a number" from "it did
        not", which is a different axis from "the number was the same".

        ``exceeds_own_noise`` is the honest reader's question -- is the gap
        between two arms larger than the run-to-run spread inside one arm? It is
        answered from :meth:`spread`, so a difference of 1 token against a spread
        of 40 cannot be quoted as a result.
        """
        spread = self.spread()
        per_metric: Dict[str, Dict[str, Any]] = {}
        for name in METRIC_NAMES:
            values: List[Optional[float]] = []
            for arm in self.arms:
                metric = arm.metrics.get(name)
                values.append(metric.value if metric is not None else None)
            measured = [value for value in values if value is not None]
            # ``None``, not ``0.0``, when no arm produced a second observation.
            # A default of zero claims the run-to-run noise WAS zero, which is
            # what let a gap "exceed" a spread measured from an empty list.
            observed_spreads = [
                row["max"] - row["min"]
                for row in spread.get(name, {}).values()
                if row["max"] is not None and row["min"] is not None
            ]
            widest = max(observed_spreads) if observed_spreads else None
            across = (max(measured) - min(measured)) if measured else None
            per_metric[name] = {
                "varies": len(set(measured)) > 1,
                "measurable": len(measured) == len(values) and bool(measured),
                "values": values,
                "widest_within_arm_spread": round(float(widest), 6) if widest is not None else None,
                "across_arm_gap": round(float(across), 6) if across is not None else None,
                "noise_measured": widest is not None,
                # An unmeasured noise floor cannot be EXCEEDED. Without this the
                # verdict is `True` on `across > 0 and widest == 0.0`, which is
                # how three arms claimed a separation measured against nothing.
                "exceeds_own_noise": bool(
                    across is not None and widest is not None and across > widest
                ),
            }
        return {
            "per_metric": per_metric,
            "metrics_that_vary": sorted(
                name for name, row in per_metric.items() if row["varies"]
            ),
            "metrics_whose_gap_exceeds_their_own_noise": sorted(
                name for name, row in per_metric.items() if row["exceeds_own_noise"]
            ),
            "note": (
                "a metric that does NOT vary has not measured the arms. "
                "'exceeds_own_noise' is the honest reader's test: the gap between arms is "
                "compared against the widest run-to-run spread observed INSIDE a single arm, "
                "so a one-token difference cannot be quoted as a result. A metric with "
                "noise_measured=false had fewer than two observations and no gap may be "
                "quoted against it."
            ),
        }

    def spread(self) -> Dict[str, Any]:
        """Return the OBSERVED variance of every metric, per arm.

        **"Never report a single run per arm" is enforced here.** A reader cannot
        see whether a difference between two arms is real without the within-arm
        spread, and a median with no spread is a number with no error bar. Every
        row carries ``n``, ``min``, ``median``, ``max`` and ``distinct`` -- and
        ``distinct == 1`` is the flag that says this arm produced no variance at
        all, which is worth knowing rather than hiding behind a median.

        **The values come from ``Metric.per_session`` -- the SAME per-repetition
        aggregates the arm's median is computed from.** Reading them off the
        repetition dicts instead looked correct and reported ``n: 0`` for every
        metric of every arm, because a repetition record holds its numbers under
        ``per_task_metrics`` rather than at the top level. An empty list has
        ``distinct == 0`` and ``min``/``max`` of ``None``, so
        ``widest_within_arm_spread`` silently became ``0.0`` -- every arm's gap
        then "exceeded" a spread measured from nothing, and the report claimed
        **zero variance** on a run that had measured none.

        **``no_variance`` is ``False`` when nothing was observed.** Zero
        observations is not evidence of stability; it is the absence of evidence,
        and ``len(set([])) <= 1`` conflates the two.
        """
        out: Dict[str, Any] = {}
        for name in METRIC_NAMES:
            per_arm: Dict[str, Any] = {}
            for arm in self.arms:
                metric = arm.metrics.get(name)
                observed = (
                    tuple(metric.per_session)
                    if metric is not None and metric.measured
                    else ()
                )
                values = [float(value) for value in observed if value is not None]
                per_arm[arm.arm] = {
                    "n": len(values),
                    "min": min(values) if values else None,
                    "median": round(statistics.median(values), 6) if values else None,
                    "max": max(values) if values else None,
                    "distinct": len(set(values)),
                    # ``None`` rather than ``False`` for an unobserved metric:
                    # "no variance" and "nothing measured" are different claims.
                    "no_variance": (len(set(values)) == 1) if values else None,
                    "values": values,
                }
            out[name] = per_arm
        return out

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "suite": "multi-session",
            "sessions": int(self.sessions),
            "repetitions": int(self.repetitions),
            "session_turns": int(self.session_turns),
            "arms": [arm.to_dict() for arm in self.arms],
            "arms_asked_for": [arm.arm for arm in self.arms],
            "arms_available": sorted(MULTI_SESSION_ARMS),
            "metrics": list(METRIC_NAMES),
            "opt_in_keys": list(OPT_IN_KEYS),
            "opt_in_keys_in_defaults": list(opt_in_keys_not_in_defaults()),
            "model_quality_measured": MODEL_QUALITY_MEASURED,
            "chars_per_token": CHARS_PER_TOKEN,
            "token_estimator": "heuristic (chars/4), NOT a tokenizer",
            "verification_lane": "host pytest; the nightly lane supplies the Docker repetitions",
            "measured": self.measured,
            "arm_realness": arm_realness(),
            "control_arithmetic": control_arm_turn_arithmetic(),
            "discrimination": self.discrimination(),
            "isolation": isolation_summary(self.arms),
            "spread": self.spread(),
            "repetitions_per_arm": int(self.repetitions),
            "sessions_per_effort": int(self.sessions),
            "session_turn_budget": int(self.session_turns),
            "task_slugs": list(TASK_SLUGS),
            "task_shapes": {
                slug: ("dependency_chain" if slug.endswith("cascade") else "independent_features")
                for slug in TASK_SLUGS
            },
            "task_shapes_not_covered": list(TASK_SHAPES_NOT_COVERED),
            "repetition_meaning": (
                "one repetition is one pass over EVERY task; a repetition is not a "
                "repetition of one task, and per-task values are published beside each "
                "aggregate because a mean across two tasks is a different quantity from "
                "either task's number"
            ),
            "honest_note": (
                f"{len(TASK_SLUGS)} task(s), covering "
                f"{len({('dependency_chain' if s.endswith('cascade') else 'independent_features') for s in TASK_SLUGS})} "
                "of the cross-session shapes. A null result here is a null result for THESE "
                "shapes and not for long-horizon autonomy in general; the shapes this table "
                "does NOT cover are named in task_shapes_not_covered."
            ),
        }

    def render(self) -> str:
        """Return the report as plain lines."""
        lines = [
            "multi-session evaluation",
            f"  sessions per effort : {self.sessions}",
            f"  repetitions        : {self.repetitions}",
            f"  session turn budget: {self.session_turns}",
            f"  token estimator    : heuristic (chars/{int(CHARS_PER_TOKEN)}), not a tokenizer",
            "  model quality      : NOT MEASURED (scripted boundary)",
            "",
        ]
        for arm in self.arms:
            lines.append(arm.render())
            lines.append("")
        separating = self.discrimination()["metrics_that_vary"]
        beyond_noise = self.discrimination()["metrics_whose_gap_exceeds_their_own_noise"]
        lines.append(
            "metrics that SEPARATE the arms: "
            + (", ".join(separating) if separating else "(none - see the discrimination receipt)")
        )
        lines.append(
            "  of those, gap EXCEEDS its own within-arm spread: "
            + (", ".join(beyond_noise) if beyond_noise else "(none)")
        )
        reality = arm_realness()
        lines.append(
            f"arms verified real: {reality['all_arms_real']} "
            + "; ".join(
                f"{row['arm']}=1 var ({row['mechanism']}, read={row['mechanism_read']})"
                for row in reality["arms"]
            )
        )
        control = control_arm_turn_arithmetic()
        lines.append(f"control arithmetic: {control['arithmetic']}")
        no_variance = sorted(
            f"{name}/{arm.arm}"
            for name, rows in self.spread().items()
            for arm in self.arms
            if (rows.get(arm.arm) or {}).get("no_variance")
            and (rows.get(arm.arm) or {}).get("n", 0) > 1
        )
        lines.append(
            "metrics with NO variance across repetitions: "
            + (", ".join(no_variance) if no_variance else "(none)")
        )
        # The line that would have caught the `spread()` defect: a reader who sees
        # "(none)" above has to be able to tell "it moved every time" from "it was
        # never compared against itself".
        thin = sorted(
            f"{name}/{arm.arm} (n={row.get('n')})"
            for name, rows in self.spread().items()
            for arm in self.arms
            if (row := (rows.get(arm.arm) or {})) and row.get("n", 0) < 2
        )
        lines.append(
            "metrics with NO measurable noise floor (<2 observations): "
            + (", ".join(thin) if thin else "(none)")
        )
        return "\n".join(lines).rstrip()


def run_multi_session(
    arms: Sequence[str] = ("baseline",),
    *,
    task_slugs: Sequence[str] = (),
    sessions: int = DEFAULT_SESSIONS,
    session_turns: int = DEFAULT_SESSION_TURNS,
    repetitions: int = DEFAULT_REPETITIONS,
    gate: Optional[Callable[[Mapping[str, str], str], bool]] = None,
    out_root: Any = "",
) -> MultiSessionReport:
    """Run the evaluation for ``arms`` and return the report."""
    return MultiSessionReport(
        arms=tuple(
            run_arm(
                arm,
                task_slugs=task_slugs,
                sessions=sessions,
                session_turns=session_turns,
                repetitions=repetitions,
                gate=gate,
                out_root=out_root,
            )
            for arm in arms
        ),
        sessions=int(sessions),
        repetitions=int(repetitions),
        session_turns=int(session_turns),
    )


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """Print the report. Exits 2 when no arm produced a measurement.

    A suite that runs cleanly and measures nothing is ``not_run``, and the exit
    code says so -- a driver that always exits 0 is a measurement, not a gate.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m evals.multi_session")
    parser.add_argument(
        "--arms",
        default=",".join(MULTI_SESSION_ARMS),
        help=f"comma-separated; known arms are {','.join(sorted(MULTI_SESSION_ARMS))}",
    )
    parser.add_argument(
        "--tasks",
        default=",".join(TASK_SLUGS),
        help=f"comma-separated task slugs; known tasks are {','.join(TASK_SLUGS)}",
    )
    parser.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS)
    parser.add_argument("--session-turns", type=int, default=DEFAULT_SESSION_TURNS)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out-root", default="", help="where the per-repetition repositories go")
    args = parser.parse_args(list(argv) if argv is not None else None)

    arms = [name.strip() for name in str(args.arms).split(",") if name.strip()]
    task_slugs = [name.strip() for name in str(args.tasks).split(",") if name.strip()]
    report = run_multi_session(
        arms,
        task_slugs=task_slugs,
        sessions=args.sessions,
        session_turns=args.session_turns,
        repetitions=args.repetitions,
        out_root=args.out_root,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(report.render())
    return 0 if report.measured else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
